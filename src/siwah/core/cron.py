"""Local cron-style scheduled agent jobs.

Jobs live in ``~/.siwah/harness/cron.json``. Schedules use the classic
five-field cron syntax and are evaluated in the machine's local timezone.
Automatic execution is opt-in through ``[cron].enabled``; every run is also
bounded by an explicit workspace, a timeout, a per-job allowance, and a daily
allowance. Nothing is delivered to external platforms.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..harness.memory import redact_secrets
from ..paths import harness_dir, write_private
from ..permissions.policy import Approval, PermissionRequest
from .storage import Store

if TYPE_CHECKING:
    from ..config import Config
    from ..providers.registry import RoleRouter
    from .loop import AgentLoop

CRON_VERSION = 1
MAX_RUNS = 20
ALLOWED_KINDS = frozenset({"bash", "write", "network"})
_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_FIELD = re.compile(r"^[0-9*/,-]+$")

CronStatus = Literal["ok", "error", "timeout", "aborted"]


class CronError(RuntimeError):
    """Invalid cron configuration or storage."""


class CronBusy(CronError):
    """Another cron run currently holds the execution lock."""


def _now() -> datetime:
    return datetime.now().astimezone()


def _now_iso() -> str:
    return _now().isoformat()


def _cron_base(root: Path | str | None = None) -> Path:
    return Path(root) if root is not None else harness_dir()


def cron_path(root: Path | str | None = None) -> Path:
    return _cron_base(root) / "cron.json"


def cron_run_lock_path(root: Path | str | None = None) -> Path:
    return _cron_base(root) / "cron.run.lock"


def cron_store_lock_path(root: Path | str | None = None) -> Path:
    return _cron_base(root) / "cron.store.lock"


# ---------------------------------------------------------------------------
# Schedule parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CronSchedule:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    day_restricted: bool
    weekday_restricted: bool
    expression: str


def _parse_field(value: str, low: int, high: int, name: str) -> frozenset[int]:
    if not value or not _FIELD.fullmatch(value):
        raise CronError(f"schedule {name} field is invalid: {value!r}")
    values: set[int] = set()
    for part in value.split(","):
        if not part:
            raise CronError(f"schedule {name} field has an empty list item")
        step = 1
        base = part
        if "/" in part:
            base, _, step_text = part.partition("/")
            if not step_text.isdigit() or int(step_text) <= 0:
                raise CronError(f"schedule {name} step must be a positive integer")
            step = int(step_text)
        if base == "*":
            start, end = low, high
        elif "-" in base:
            start_text, _, end_text = base.partition("-")
            if not start_text.isdigit() or not end_text.isdigit():
                raise CronError(f"schedule {name} range is invalid: {part!r}")
            start, end = int(start_text), int(end_text)
        else:
            if not base.isdigit():
                raise CronError(f"schedule {name} value is invalid: {part!r}")
            start = end = int(base)
        if start < low or end > high or start > end:
            raise CronError(f"schedule {name} value must be between {low} and {high}")
        values.update(range(start, end + 1, step))
    if not values:
        raise CronError(f"schedule {name} field is empty")
    return frozenset(values)


def parse_schedule(expression: str) -> CronSchedule:
    """Parse a five-field cron expression.

    Supported syntax: ``*``, ``n``, ``a-b``, ``*/step``, ``a-b/step``, and
    comma lists. Month and weekday names are intentionally not supported so
    the schedule means the same thing on every platform.
    """

    fields = expression.split()
    if len(fields) != 5:
        raise CronError("schedule must contain exactly 5 fields: minute hour day month weekday")
    minutes = _parse_field(fields[0], 0, 59, "minute")
    hours = _parse_field(fields[1], 0, 23, "hour")
    days = _parse_field(fields[2], 1, 31, "day-of-month")
    months = _parse_field(fields[3], 1, 12, "month")
    raw_weekdays = _parse_field(fields[4], 0, 7, "day-of-week")
    weekdays = frozenset(0 if item == 7 else item for item in raw_weekdays)
    return CronSchedule(
        minutes=minutes,
        hours=hours,
        days=days,
        months=months,
        weekdays=weekdays,
        day_restricted=fields[2] != "*",
        weekday_restricted=fields[4] != "*",
        expression=" ".join(fields),
    )


def next_run(schedule: CronSchedule, after: datetime) -> datetime | None:
    """Return the next local wall-clock time strictly after ``after``.

    Classic cron treats day-of-month and day-of-week as an OR when both are
    restricted. Scanning is bounded to five years so an impossible schedule
    fails cleanly instead of looping forever.
    """

    start = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    first_day = start.date()
    for offset in range(366 * 5):
        day = first_day + timedelta(days=offset)
        if day.month not in schedule.months:
            continue
        day_match = day.day in schedule.days
        weekday_match = ((day.weekday() + 1) % 7) in schedule.weekdays
        if schedule.day_restricted and schedule.weekday_restricted:
            matched = day_match or weekday_match
        elif schedule.day_restricted:
            matched = day_match
        elif schedule.weekday_restricted:
            matched = weekday_match
        else:
            matched = True
        if not matched:
            continue
        for hour in sorted(schedule.hours):
            for minute in sorted(schedule.minutes):
                candidate = start.replace(
                    year=day.year,
                    month=day.month,
                    day=day.day,
                    hour=hour,
                    minute=minute,
                )
                if candidate >= start:
                    return candidate
    return None


# ---------------------------------------------------------------------------
# Models and storage
# ---------------------------------------------------------------------------


class CronRun(BaseModel):
    model_config = ConfigDict(extra="ignore")

    started_at: str
    finished_at: str
    status: CronStatus = "ok"
    session_id: str | None = None
    steps: int = 0
    allowance: float = 0.0
    error: str | None = None


class CronJob(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    schedule: str
    cwd: str
    prompt: str
    model: str | None = None
    enabled: bool = True
    allow: list[str] = Field(default_factory=list)
    max_steps: int = 30
    max_allowance: float = 0.5
    timeout_s: float = 900.0
    created_at: str = Field(default_factory=_now_iso)
    next_run_at: str | None = None
    last_run_at: str | None = None
    last_status: str | None = None
    last_session_id: str | None = None
    last_error: str | None = None
    runs: list[CronRun] = Field(default_factory=list)


class CronUsage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    date: str = ""
    allowance: float = 0.0


class CronState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: int = CRON_VERSION
    jobs: list[CronJob] = Field(default_factory=list)
    usage: CronUsage = Field(default_factory=CronUsage)


def validate_job(job: CronJob) -> None:
    if not _JOB_ID.fullmatch(job.id):
        raise CronError(
            "job id must start with a letter or digit and use only letters, digits, dot, dash, underscore"
        )
    parse_schedule(job.schedule)
    if not Path(job.cwd).expanduser().is_dir():
        raise CronError(f"workspace does not exist: {job.cwd}")
    if not job.prompt.strip():
        raise CronError("prompt cannot be empty")
    if job.max_steps < 1:
        raise CronError("max_steps must be at least 1")
    if not math.isfinite(job.max_allowance) or job.max_allowance <= 0:
        raise CronError("max_allowance must be a finite number greater than 0")
    if not math.isfinite(job.timeout_s) or job.timeout_s <= 0:
        raise CronError("timeout_s must be a finite number greater than 0")
    unknown = sorted(set(job.allow) - ALLOWED_KINDS)
    if unknown:
        raise CronError(f"unknown permission kinds: {', '.join(unknown)}")


def make_job_id(prompt: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", prompt.casefold()).strip("-")[:24].strip("-")
    return f"{slug or 'job'}-{secrets.token_hex(3)}"


class CronStore:
    """JSON-backed job store with an atomic write lock."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = _cron_base(root)
        self.path = cron_path(self.root)
        self._lock_path = cron_store_lock_path(self.root)

    def load(self) -> CronState:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return CronState()
        except OSError as exc:
            raise CronError(f"cannot read {self.path}: {exc}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CronError(f"cannot parse {self.path}: {exc}") from exc
        try:
            return CronState.model_validate(payload)
        except ValueError as exc:
            raise CronError(f"invalid cron state in {self.path}: {exc}") from exc

    def _save(self, state: CronState) -> None:
        body = json.dumps(state.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
        write_private(self.path, body)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with exclusive_lock(self._lock_path, wait_s=5.0, stale_s=30.0):
            yield

    def jobs(self) -> list[CronJob]:
        return self.load().jobs

    def get(self, job_id: str) -> CronJob | None:
        return next((job for job in self.load().jobs if job.id == job_id), None)

    def add(self, job: CronJob) -> CronJob:
        validate_job(job)
        with self._locked():
            state = self.load()
            if any(existing.id == job.id for existing in state.jobs):
                raise CronError(f"cron job already exists: {job.id}")
            state.jobs.append(job)
            self._save(state)
        return job

    def remove(self, job_id: str) -> bool:
        with self._locked():
            state = self.load()
            kept = [job for job in state.jobs if job.id != job_id]
            if len(kept) == len(state.jobs):
                return False
            state.jobs = kept
            self._save(state)
        return True

    def set_enabled(self, job_id: str, enabled: bool) -> CronJob | None:
        with self._locked():
            state = self.load()
            job = next((item for item in state.jobs if item.id == job_id), None)
            if job is None:
                return None
            job.enabled = enabled
            self._save(state)
            return job

    def update_job(
        self,
        job_id: str,
        mutate: Callable[[CronJob], None],
        *,
        allowance: float = 0.0,
        day: date | None = None,
    ) -> CronJob | None:
        """Reload, mutate one job, and optionally add allowance usage."""

        with self._locked():
            state = self.load()
            job = next((item for item in state.jobs if item.id == job_id), None)
            if job is not None:
                mutate(job)
            if allowance > 0:
                today = (day or _now().date()).isoformat()
                if state.usage.date != today:
                    state.usage = CronUsage(date=today, allowance=0.0)
                state.usage.allowance += allowance
            if job is not None or allowance > 0:
                self._save(state)
            return job

    def used_today(self, *, today: date | None = None) -> float:
        state = self.load()
        key = (today or _now().date()).isoformat()
        return state.usage.allowance if state.usage.date == key else 0.0


def apply_run(job: CronJob, record: CronRun, *, next_run_at: str | None) -> None:
    job.last_run_at = record.finished_at
    job.last_status = record.status
    job.last_session_id = record.session_id
    job.last_error = record.error
    if next_run_at is not None:
        job.next_run_at = next_run_at
    job.runs = [*job.runs[-(MAX_RUNS - 1) :], record]


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == 259  # STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _stale_lock(path: Path, stale_s: float) -> bool:
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    if age < 1.0:
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return age >= stale_s
        pid = int(payload.get("pid", 0))
    except (OSError, ValueError, TypeError):
        return age >= stale_s
    if not _pid_alive(pid):
        return True
    return age >= max(1.0, stale_s)


@contextmanager
def exclusive_lock(path: Path, *, wait_s: float = 0.0, stale_s: float = 60.0) -> Iterator[None]:
    """Create an exclusive lock file, optionally waiting for a short time."""

    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.0, wait_s)
    descriptor: int | None = None
    while True:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if _stale_lock(path, stale_s):
                with contextlib.suppress(OSError):
                    path.unlink()
                continue
            if time.monotonic() >= deadline:
                raise CronBusy(f"another cron run is active ({path})") from None
            time.sleep(0.05)
    assert descriptor is not None
    try:
        payload = json.dumps({"pid": os.getpid(), "started_at": _now_iso()})
        os.write(descriptor, payload.encode("utf-8"))
        yield
    finally:
        os.close(descriptor)
        with contextlib.suppress(OSError):
            path.unlink()


@contextmanager
def run_lock(
    root: Path | str | None = None, *, timeout_s: float = 900.0, wait_s: float = 0.0
) -> Iterator[None]:
    with exclusive_lock(
        cron_run_lock_path(root),
        wait_s=wait_s,
        stale_s=max(60.0, timeout_s + 120.0),
    ):
        yield


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


async def execute_job(
    job: CronJob,
    *,
    config: Config,
    store: Store,
    router: RoleRouter,
    agent: AgentLoop | None = None,
    emit: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    now: datetime | None = None,
) -> CronRun:
    """Run one job in a fresh session with a bounded permission allow-list."""

    started = (now or _now()).isoformat()
    workspace = Path(job.cwd).expanduser().resolve()
    if not workspace.is_dir():
        return CronRun(
            started_at=started,
            finished_at=_now_iso(),
            status="error",
            error=f"workspace does not exist: {workspace}",
        )

    permissions = config.permissions.model_copy(
        update={
            "mode": "ask",
            # Keep explicit denials, but never let a broad allow rule silently
            # grant an unattended job more than its own allow-list.
            "rules": [rule for rule in config.permissions.rules if rule.decision == "deny"],
        }
    )
    cron_config = config.model_copy(
        update={
            "permissions": permissions,
            # Child sessions have their own allowance accounting; keep an
            # unattended run inside one session so its cap covers all spend.
            "subagents": config.subagents.model_copy(update={"enabled": False}),
        }
    )
    owned = agent is None
    if agent is None:
        from ..tools.base import ToolRegistry
        from ..tools.runtime import default_tools
        from .loop import AgentLoop

        agent = AgentLoop(
            config=cron_config,
            store=store,
            router=router,
            tools=ToolRegistry(default_tools()),
        )

    session = store.create_session(
        cwd=str(workspace),
        model=job.model or cron_config.roles.main,
        title=f"cron {job.id}: {job.prompt[:60]}",
    )
    store.update_session(session.id, metadata={"cron_job": job.id})
    allowed = set(job.allow)

    async def approve(request: PermissionRequest, _request_id: str) -> Approval:
        if request.kind == "read" or request.kind in allowed:
            return Approval(decision="session")
        return Approval(
            decision="deny",
            reason=f"cron job {job.id} does not allow {request.kind}",
        )

    async def no_emit(_event_type: str, _data: dict[str, Any]) -> None:
        return None

    sink = emit or no_emit
    steps = 0
    allowance = 0.0
    error: str | None = None
    status: CronStatus = "ok"
    try:
        result = await asyncio.wait_for(
            agent.run(
                session,
                job.prompt,
                emit=sink,
                permission_resolver=approve,
                ask_user=None,
                model_override=job.model,
                max_steps=job.max_steps,
                max_allowance=job.max_allowance,
            ),
            timeout=job.timeout_s,
        )
        steps = result.steps
        allowance = result.usage.allowance
        if result.aborted:
            status = "aborted"
            error = "run aborted"
        elif result.error:
            status = "error"
            error = result.error
    except TimeoutError:
        status = "timeout"
        error = f"timed out after {job.timeout_s:g}s"
        allowance = store.usage_totals(session.id).allowance
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        status = "error"
        error = str(exc)
        with contextlib.suppress(Exception):
            allowance = store.usage_totals(session.id).allowance
    finally:
        with contextlib.suppress(Exception):
            store.update_session(session.id, status="idle")
        if owned:
            with contextlib.suppress(Exception):
                await agent.close_jobs()

    return CronRun(
        started_at=started,
        finished_at=_now_iso(),
        status=status,
        session_id=session.id,
        steps=steps,
        allowance=round(max(0.0, allowance), 8),
        error=redact_secrets(error) if error else None,
    )


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

JobExecutor = Callable[..., Awaitable[CronRun]]


class CronScheduler:
    """Tick-driven scheduler used by the daemon and by tests."""

    def __init__(
        self,
        config: Config,
        store: Store,
        router: RoleRouter,
        *,
        cron_store: CronStore | None = None,
        executor: JobExecutor | None = None,
        emit: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.router = router
        self.cron_store = cron_store or CronStore()
        self.executor = executor or execute_job
        self.emit = emit
        self._task: asyncio.Task[Any] | None = None
        self._ticking = False

    @property
    def enabled(self) -> bool:
        return bool(self.config.cron.enabled)

    async def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="siwah-cron")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A bad job or transient storage error must not kill the daemon
                # scheduler; the next tick retries after the configured delay.
                pass
            await asyncio.sleep(max(5, self.config.cron.tick_seconds))

    def _reschedule(self, job: CronJob, now: datetime, error: str | None = None) -> None:
        schedule = parse_schedule(job.schedule)
        upcoming = next_run(schedule, now)

        def mutate(item: CronJob) -> None:
            if upcoming is not None:
                item.next_run_at = upcoming.isoformat()
            if error is not None:
                item.last_error = error

        self.cron_store.update_job(job.id, mutate)

    async def tick(self) -> list[CronRun]:
        if self._ticking:
            return []
        self._ticking = True
        records: list[CronRun] = []
        try:
            state = self.cron_store.load()
            now = _now()
            used = state.usage.allowance if state.usage.date == now.date().isoformat() else 0.0
            jobs = sorted(state.jobs, key=lambda item: item.next_run_at or "")
            for job in jobs:
                if not job.enabled or not job.next_run_at:
                    continue
                try:
                    due = datetime.fromisoformat(job.next_run_at)
                except ValueError:
                    self._reschedule(job, now, "invalid next_run_at")
                    continue
                if due.tzinfo is None:
                    due = due.replace(tzinfo=now.tzinfo)
                if due > now:
                    continue
                grace = max(60.0, self.config.cron.tick_seconds * 2.0)
                if not self.config.cron.catch_up and (now - due).total_seconds() > grace:
                    self._reschedule(job, now)
                    continue
                remaining = max(0.0, self.config.cron.max_daily_allowance - used)
                if remaining <= 0:
                    self._reschedule(job, now, "daily allowance exhausted")
                    continue
                effective = job.model_copy(
                    update={"max_allowance": min(job.max_allowance, remaining)}
                )
                try:
                    with run_lock(self.cron_store.root, timeout_s=effective.timeout_s + 120.0):
                        record = await self.executor(
                            effective,
                            config=self.config,
                            store=self.store,
                            router=self.router,
                            emit=self.emit,
                        )
                except CronBusy:
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    record = CronRun(
                        started_at=_now_iso(),
                        finished_at=_now_iso(),
                        status="error",
                        error=redact_secrets(str(exc)),
                    )
                upcoming = next_run(parse_schedule(job.schedule), _now())
                next_at = upcoming.isoformat() if upcoming is not None else None

                def mutate(
                    item: CronJob, run: CronRun = record, next_run_at: str | None = next_at
                ) -> None:
                    apply_run(item, run, next_run_at=next_run_at)

                self.cron_store.update_job(job.id, mutate, allowance=record.allowance)
                used += record.allowance
                records.append(record)
            return records
        finally:
            self._ticking = False


__all__ = [
    "ALLOWED_KINDS",
    "CronBusy",
    "CronError",
    "CronJob",
    "CronRun",
    "CronSchedule",
    "CronScheduler",
    "CronState",
    "CronStore",
    "CronUsage",
    "apply_run",
    "cron_path",
    "cron_run_lock_path",
    "cron_store_lock_path",
    "execute_job",
    "exclusive_lock",
    "make_job_id",
    "next_run",
    "parse_schedule",
    "run_lock",
    "validate_job",
]
