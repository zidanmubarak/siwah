"""Daemon runtime: sessions, event buses, runs, and permission waiters."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import threading
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..adapt.observer import _CORRECTION_HINTS
from ..adapt.state import load_state
from ..adapt.trigger import EvolutionTrigger
from ..auth import AuthError, AuthStore
from ..config import Config, ConfigError, update_agent_effort
from ..core.background import BackgroundResult, render_background_result
from ..core.checkpoints import CheckpointError, CheckpointManager
from ..core.context import (
    compaction_prompt,
    estimate_tokens,
    fallback_summary,
)
from ..core.cron import CronScheduler
from ..core.loop import AgentLoop, RunResult
from ..core.questions import Question
from ..core.review import BackgroundReview, review_prompt
from ..core.storage import Session, Store
from ..core.timing import RunTiming, active_timing
from ..core.types import Message, MessageAttachment, ModelInfo, Usage
from ..harness.lessons import read_lessons
from ..harness.memory import (
    init_agents_file,
    init_project_memory,
    quick_note,
    read_memory,
    redact_secrets,
)
from ..harness.skill_usage import load_usage
from ..harness.skills import list_skills, read_skill
from ..permissions.policy import Approval, PermissionPolicy, PermissionRequest
from ..providers.base import ProviderError
from ..providers.openai_responses import supports_chatgpt_login
from ..providers.openrouter_auth import supports_browser_login
from ..providers.profiles import load_profile
from ..providers.registry import ProviderRegistry, RoleRouter
from ..sandbox import Sandbox
from ..tools.base import EventEmitter, ToolContext, ToolRegistry
from ..tools.runtime import BashTool, default_tools
from .events import EventBus
from .schema import QuestionRequest

PERMISSION_TIMEOUT_S = 300.0
QUESTION_TIMEOUT_S = 300.0
MAX_QUEUED_PROMPTS = 16
QUEUE_RETRY_DELAY_S = 0.5
BACKGROUND_RETRY_DELAY_S = 0.5


@dataclass(frozen=True)
class QueuedPrompt:
    id: str
    content: str
    attachments: list[MessageAttachment]


class RuntimeError_(RuntimeError):
    pass


class ShellPermissionRequired(RuntimeError):
    def __init__(self, preview: str) -> None:
        super().__init__("Shell command requires approval")
        self.preview = preview


class ShellDenied(RuntimeError):
    pass


class ConfirmationRequired(RuntimeError):
    def __init__(self, checkpoint: dict[str, Any]) -> None:
        super().__init__("Confirmation required before rewinding the workspace")
        self.checkpoint = checkpoint


class DaemonRuntime:
    def __init__(
        self,
        config: Config,
        *,
        auth: AuthStore | None = None,
        store: Store | None = None,
        registry: ProviderRegistry | None = None,
        router: RoleRouter | None = None,
        loop: AgentLoop | None = None,
    ) -> None:
        self.config = config
        self.store = store or Store()
        self.auth = auth or AuthStore()
        self.registry = registry or ProviderRegistry(config, self.auth)
        self.router = router or RoleRouter(config, self.registry)
        self.loop = loop or AgentLoop(
            config=config,
            store=self.store,
            router=self.router,
            tools=ToolRegistry(default_tools()),
        )
        self.evolution_trigger = EvolutionTrigger(
            config.evolution,
            enabled=bool(config.evolution.auto and (config.roles.reflector or config.roles.main)),
        )
        self.cron = CronScheduler(config, self.store, self.router)
        self.started_at = datetime.now(UTC).isoformat()
        self._buses: dict[str, EventBus] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._queues: dict[str, deque[QueuedPrompt]] = {}
        self._queue_blocked: dict[str, str] = {}
        self._queue_retries: dict[str, asyncio.TimerHandle] = {}
        self._closing = False
        self._steering: dict[str, dict[str, str]] = {}
        self._active_run_ids: dict[str, str] = {}
        self._waiters: dict[str, asyncio.Future[Approval]] = {}
        self._waiter_sessions: dict[str, str] = {}
        self._questions: dict[str, tuple[QuestionRequest, asyncio.Future[str | None]]] = {}
        self._aborting: set[str] = set()
        self._background_pending: set[str] = set()
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._background_retries: dict[str, asyncio.TimerHandle] = {}
        self._watermarks: dict[str, int] = {}
        self._operation_lock = threading.RLock()
        self._workspace_operations: set[Path] = set()

    # ------------------------------------------------------------------
    # Sessions and runs
    # ------------------------------------------------------------------
    def create_session(
        self,
        *,
        cwd: str | None,
        model: str | None,
        mode: str = "build",
        title: str | None = None,
    ) -> Session:
        workspace = Path(cwd or Path.cwd()).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError(f"Workspace does not exist: {workspace}")
        if model is None and self.config.roles.main is not None:
            model = self.config.roles.main
        return self.store.create_session(cwd=str(workspace), model=model, mode=mode, title=title)

    def get_session(self, session_id: str) -> Session | None:
        return self.store.get_session(session_id)

    def is_running(self, session_id: str) -> bool:
        task = self._tasks.get(session_id)
        return task is not None and not task.done()

    def is_waiting_permission(self, session_id: str) -> bool:
        return session_id in self._waiter_sessions.values()

    def list_sessions(self, limit: int = 50) -> list[Session]:
        return self.store.list_sessions(limit=limit)

    def bus(self, session_id: str) -> EventBus:
        return self._buses.setdefault(
            session_id, EventBus(session_id, retention=self.config.server.event_retention)
        )

    def last_event_seq(self, session_id: str) -> int:
        bus = self._buses.get(session_id)
        if bus is None:
            return 0
        task = self._tasks.get(session_id)
        if task is not None and not task.done():
            # Message deltas are streamed before the assistant row exists; a
            # watermark past them would make hydrate + replay lossy.
            return self._watermarks.get(session_id, 0)
        return bus.last_seq()

    def start_run(
        self,
        session: Session,
        content: str,
        attachments: list[MessageAttachment] | None = None,
        *,
        queued_prompt_id: str | None = None,
    ) -> tuple[str, int]:
        with self._operation_lock:
            if self._closing:
                raise RuntimeError_("Daemon is shutting down")
            workspace = Path(session.cwd).resolve()
            if any(_overlaps(workspace, root) for root in self._workspace_operations):
                raise SessionBusy("Workspace is busy with another operation")
            return self._start_run(session, content, attachments, queued_prompt_id=queued_prompt_id)

    def submit_run(
        self,
        session: Session,
        content: str,
        attachments: list[MessageAttachment] | None = None,
    ) -> tuple[str, int, str]:
        """Start a run, or queue it behind the active run for this session."""

        with self._operation_lock:
            if self._closing:
                raise RuntimeError_("Daemon is shutting down")
            if not self.is_running(session.id) and not self._queues.get(session.id):
                run_id, event_id = self.start_run(session, content, attachments)
                return run_id, event_id, "running"
            queue = self._queues.setdefault(session.id, deque())
            if len(queue) >= MAX_QUEUED_PROMPTS:
                raise RuntimeError_(
                    f"At most {MAX_QUEUED_PROMPTS} prompts can be queued for a session"
                )
            run_id = f"queued_{uuid.uuid4().hex}"
            queue.append(QueuedPrompt(run_id, content, list(attachments or [])))
            return run_id, self.bus(session.id).last_seq(), "queued"

    def queue_snapshot(self, session_id: str) -> dict[str, Any]:
        with self._operation_lock:
            blocked = self._queue_blocked.get(session_id)
            items = [
                {
                    "id": item.id,
                    "preview": " ".join(redact_secrets(item.content).split())[:180],
                    "attachment_count": len(item.attachments),
                    **({"blocked_reason": blocked} if index == 0 and blocked else {}),
                }
                for index, item in enumerate(self._queues.get(session_id, ()))
            ]
            return {"event_seq": self.bus(session_id).last_seq(), "items": items}

    async def publish_queue(self, session_id: str) -> int:
        bus = self.bus(session_id)
        event = await bus.publish(
            "queue.changed", {"items": self.queue_snapshot(session_id)["items"]}
        )
        return event.seq

    def resolve_correction(self, session_id: str, message_id: str) -> bool:
        return self.store.delete_unapplied_correction(session_id, message_id)

    async def remove_queued_prompt(self, session_id: str, prompt_id: str) -> QueuedPrompt | None:
        with self._operation_lock:
            queue = self._queues.get(session_id)
            item = next((item for item in queue or () if item.id == prompt_id), None)
            if item is None or queue is None:
                return None
            removed_blocked = queue[0] is item and session_id in self._queue_blocked
            queue.remove(item)
            if not queue:
                self._queues.pop(session_id, None)
            if removed_blocked:
                self._clear_queue_block(session_id)
                self._drain_queue(session_id)
        await self.publish_queue(session_id)
        self._maybe_start_background(session_id)
        return item

    def active_run_id(self, session_id: str) -> str | None:
        return self._active_run_ids.get(session_id) if self.is_running(session_id) else None

    def steer(self, session_id: str, content: str, active_run_id: str) -> tuple[str, int]:
        with self._operation_lock:
            task = self._tasks.get(session_id)
            inbox = self._steering.get(active_run_id)
            if (
                not task
                or task.done()
                or inbox is None
                or self._active_run_ids.get(session_id) != active_run_id
            ):
                raise RuntimeError_("This run has already finished or changed.")
            if len(inbox) >= MAX_QUEUED_PROMPTS:
                raise RuntimeError_("Too many pending corrections.")
            message_id = f"msg_{uuid.uuid4().hex}"
            inbox[message_id] = content
            return message_id, self.bus(session_id).last_seq()

    def _start_run(
        self,
        session: Session,
        content: str,
        attachments: list[MessageAttachment] | None = None,
        *,
        queued_prompt_id: str | None = None,
        continuation: bool = False,
    ) -> tuple[str, int]:
        if not session.cwd:
            raise ValueError("Session has no workspace")
        existing = self._tasks.get(session.id)
        if existing is not None and not existing.done():
            raise RuntimeError_("A run is already active for this session")
        if not session.title:
            title = content.strip().splitlines()[0][:80] if content.strip() else "Image message"
            if title:
                self.store.update_session(session.id, title=title)
        run_id = f"run_{uuid.uuid4().hex}"
        self._active_run_ids[session.id] = run_id
        self._aborting.discard(session.id)
        self._steering[run_id] = {}
        bus = self.bus(session.id)
        ended = False

        async def emit(event_type: str, data: dict[str, Any]) -> None:
            nonlocal ended
            if event_type == "run.end":
                await self._publish_unapplied(
                    session.id, run_id, self._save_unapplied(session.id, run_id)
                )
            if queued_prompt_id and event_type == "message.start" and data.get("role") == "user":
                data = {
                    **data,
                    "queued_prompt_id": queued_prompt_id,
                    "content": redact_secrets(content),
                }
            envelope = await bus.publish(event_type, data, turn_id=run_id)
            if event_type == "run.end":
                ended = True
            if event_type in {
                "message.end",
                "tool.end",
                "run.end",
                "plan",
                "checkpoint",
                "steering.applied",
            }:
                self._watermarks[session.id] = envelope.seq

        async def run() -> None:
            try:
                if queued_prompt_id:
                    await self.publish_queue(session.id)
                await self._run(
                    session,
                    content,
                    attachments or [],
                    emit,
                    run_id,
                    continuation=continuation,
                )
            except asyncio.CancelledError:
                if not ended:
                    self.store.update_session(session.id, status="idle")
                    await emit("run.end", {"reason": "aborted"})
                raise

        task = asyncio.create_task(run())
        self._tasks[session.id] = task
        task.add_done_callback(lambda finished: self._forget_task(session.id, finished, run_id))
        return run_id, bus.last_seq()

    def _forget_task(self, session_id: str, task: asyncio.Task[Any], run_id: str) -> None:
        with self._operation_lock:
            if self._tasks.get(session_id) is task:
                self._tasks.pop(session_id, None)
                self._active_run_ids.pop(session_id, None)
            self._aborting.discard(session_id)
            remaining = self._save_unapplied(session_id, run_id)
        if remaining:
            asyncio.create_task(self._publish_unapplied(session_id, run_id, remaining))
        self._drain_queue(session_id)
        self._maybe_start_background(session_id)
        with contextlib.suppress(Exception):
            from ..harness.skill_usage import maybe_curate

            maybe_curate()

    def _save_unapplied(self, session_id: str, run_id: str) -> dict[str, str]:
        with self._operation_lock:
            remaining = self._steering.pop(run_id, {})
            for message_id, content in remaining.items():
                self.store.record_unapplied_correction(session_id, message_id, content)
            return remaining

    async def _publish_unapplied(
        self, session_id: str, run_id: str, remaining: dict[str, str]
    ) -> None:
        for message_id, content in remaining.items():
            await self.bus(session_id).publish(
                "steering.dropped",
                {"message_id": message_id, "content": redact_secrets(content)},
                turn_id=run_id,
            )

    def _drain_queue(self, session_id: str) -> None:
        """Start the next queued prompt after the active task has finished."""

        with self._operation_lock:
            if self._closing or self.is_running(session_id):
                return
            queue = self._queues.get(session_id)
            if not queue:
                return
            session = self.store.get_session(session_id)
            if session is None:
                return
            prompt = queue[0]
            try:
                self.start_run(
                    session, prompt.content, prompt.attachments, queued_prompt_id=prompt.id
                )
            except Exception as exc:
                reason = redact_secrets(str(exc))
                if self._queue_blocked.get(session_id) != reason:
                    self._queue_blocked[session_id] = reason
                    asyncio.create_task(self.publish_queue(session_id))
                if isinstance(exc, SessionBusy) and session_id not in self._queue_retries:
                    self._queue_retries[session_id] = asyncio.get_running_loop().call_later(
                        QUEUE_RETRY_DELAY_S, self._retry_queue, session_id
                    )
                return
            queue.popleft()
            self._clear_queue_block(session_id)
            if not queue:
                self._queues.pop(session_id, None)

    def _retry_queue(self, session_id: str) -> None:
        self._queue_retries.pop(session_id, None)
        self._drain_queue(session_id)

    def _clear_queue_block(self, session_id: str) -> None:
        self._queue_blocked.pop(session_id, None)
        retry = self._queue_retries.pop(session_id, None)
        if retry is not None:
            retry.cancel()

    # ------------------------------------------------------------------
    # Background results
    # ------------------------------------------------------------------
    def _track_background_task(self, task: asyncio.Task[Any]) -> None:
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _deliver_background_result(self, result: BackgroundResult) -> None:
        if self._closing:
            return
        session = self.store.get_session(result.session_id)
        if session is None:
            return
        text = redact_secrets(render_background_result(result))
        message_id = f"msg_{uuid.uuid4().hex}"
        self.store.append_message(
            session.id,
            Message(role="system", content=text),
            message_id=message_id,
        )
        self.store.append_trajectory(
            session.id,
            {
                "type": "background",
                "message_id": message_id,
                "kind": result.kind,
                "actor_id": result.actor_id,
                "status": result.status,
            },
        )
        bus = self.bus(session.id)
        event = {
            "message_id": message_id,
            "role": "system",
            "content": text,
            "kind": result.kind,
            "actor_id": result.actor_id,
            "status": result.status,
            "title": redact_secrets(result.title),
        }
        await bus.publish("message.start", event)
        await bus.publish("message.end", {"message_id": message_id, "finish_reason": "background"})
        await bus.publish(
            "background.result",
            {
                "kind": result.kind,
                "actor_id": result.actor_id,
                "status": result.status,
                "title": redact_secrets(result.title),
                "tool_call_id": result.tool_call_id,
            },
        )
        self._background_pending.add(session.id)
        self._maybe_start_background(session.id)

    def _maybe_start_background(self, session_id: str) -> None:
        if self._closing or session_id not in self._background_pending:
            return
        with self._operation_lock:
            if self._closing or session_id not in self._background_pending:
                return
            if self.is_running(session_id) or self._queues.get(session_id):
                return
            session = self.store.get_session(session_id)
            if session is None or not session.cwd:
                self._background_pending.discard(session_id)
                return
            try:
                self._start_run(session, "", continuation=True)
            except (SessionBusy, RuntimeError_):
                self._schedule_background_retry(session_id)
            except Exception:
                self._background_pending.discard(session_id)
            else:
                self._background_pending.discard(session_id)

    def _schedule_background_retry(self, session_id: str) -> None:
        if session_id in self._background_retries:
            return
        self._background_retries[session_id] = asyncio.get_running_loop().call_later(
            BACKGROUND_RETRY_DELAY_S, self._retry_background, session_id
        )

    def _retry_background(self, session_id: str) -> None:
        self._background_retries.pop(session_id, None)
        self._maybe_start_background(session_id)

    @contextlib.contextmanager
    def _exclusive_operation(self, session_id: str):
        """Reserve an idle workspace across awaits and worker-thread operations."""

        session = self.store.get_session(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")
        workspace = Path(session.cwd).resolve()
        with self._operation_lock:
            for active_id, task in self._tasks.items():
                active = self.store.get_session(active_id)
                if (
                    not task.done()
                    and active is not None
                    and _overlaps(workspace, Path(active.cwd).resolve())
                ):
                    raise SessionBusy("Workspace has an active run; stop it before this operation")
            if any(_overlaps(workspace, root) for root in self._workspace_operations):
                raise SessionBusy("Workspace is busy with another operation")
            self._workspace_operations.add(workspace)
        try:
            yield
        finally:
            with self._operation_lock:
                self._workspace_operations.discard(workspace)

    async def _run(
        self,
        session: Session,
        content: str,
        attachments: list[MessageAttachment],
        emit,
        run_id: str,
        *,
        continuation: bool = False,
    ) -> None:
        cancelled = False
        timing = RunTiming()
        evolution = load_state()
        tag = evolution.tag or "baseline"
        current_session = self.store.get_session(session.id) or session
        metadata = dict(current_session.metadata)
        if not metadata.get("pinned_system_prompts"):
            metadata["observation_harness_tag"] = tag
            self.store.update_session(session.id, metadata=metadata)
        matches_pinned_tag = metadata.get("observation_harness_tag") == tag
        allowance_before = self.store.usage_totals(session.id).allowance
        tools = errors = 0
        models: set[str] = set()

        async def observed_emit(event_type: str, data: dict[str, Any]) -> None:
            nonlocal tools, errors
            if event_type == "run.end":
                self.store.record_run_timing(session.id, run_id, timing.snapshot())
            if event_type == "tool.end":
                if data.get("status") != "skipped":
                    tools += 1
                    errors += int(data.get("status") != "ok")
            elif (
                event_type == "usage"
                and isinstance(data.get("model"), str)
                and data.get("step_id") != "compact"
            ):
                # Compaction reports the configured reference, not the model
                # that actually served this run. Keeping it would split one
                # valid observation into two model identities.
                models.add(data["model"])
            await emit(event_type, data)

        async def resolve_permission(request: PermissionRequest, request_id: str) -> Approval:
            return await self._resolve_permission(request, request_id, session_id=session.id)

        question_lock = asyncio.Lock()

        async def ask_user(question: Question, tool_call_id: str | None) -> str | None:
            async with question_lock:
                return await self._ask_question(session.id, question, tool_call_id, emit)

        def take_steering() -> list[tuple[str, str]]:
            with self._operation_lock:
                inbox = self._steering.get(run_id)
                if not inbox:
                    return []
                notes = list(inbox.items())
                inbox.clear()
                return notes

        def steering_waiting() -> bool:
            with self._operation_lock:
                return bool(self._steering.get(run_id))

        timing_token = active_timing.set(timing)
        run_result: RunResult | None = None
        try:
            run_result = await self.loop.run(
                session,
                content,
                emit=observed_emit,
                permission_resolver=resolve_permission,
                ask_user=ask_user,
                model_override=session.model,
                extend_budget=True,
                attachments=attachments,
                take_steering=take_steering,
                steering_waiting=steering_waiting,
                background_sink=self._deliver_background_result,
                track_background=self._track_background_task,
                continuation=continuation,
            )
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as exc:  # the loop already reports most failures
            await emit(
                "error",
                {"code": "internal_error", "message": str(exc), "retryable": False},
            )
            await observed_emit("run.end", {"reason": "error", "error": str(exc)})
        finally:
            active_timing.reset(timing_token)
            self.store.record_run_timing(session.id, run_id, timing.snapshot())
            latest_evolution = load_state()
            if matches_pinned_tag and len(models) == 1 and latest_evolution.tag == evolution.tag:
                self.store.record_run_observation(
                    run_id=run_id,
                    session=session,
                    model=next(iter(models)),
                    tag=tag,
                    tools=tools,
                    errors=errors,
                    corrected=any(hint in content.casefold() for hint in _CORRECTION_HINTS),
                    aborted=cancelled,
                    allowance=max(
                        0, self.store.usage_totals(session.id).allowance - allowance_before
                    ),
                )
            if not cancelled:
                other_active = any(
                    not task.done()
                    for session_id, task in self._tasks.items()
                    if session_id != session.id
                )
                if not other_active:
                    self.evolution_trigger.after_session(self.store, session.id)
            if (
                not cancelled
                and not continuation
                and run_result is not None
                and run_result.error is None
                and not run_result.aborted
                and self.config.agent.background_review
                and run_result.steps >= max(1, self.config.agent.background_review_min_steps)
            ):
                self._maybe_background_review(session, run_result, resolve_permission, emit)

    def _maybe_background_review(
        self,
        session: Session,
        result: RunResult,
        permission_resolver,
        emit,
    ) -> None:
        if self._closing:
            return
        calls = self.store.get_tool_calls(session.id)
        if any(call.get("name") in {"lesson_write", "skill_manage"} for call in calls[-20:]):
            return
        review = BackgroundReview(
            config=self.config,
            store=self.store,
            router=self.router,
            tools=self.loop.tools.tools(),
            parent_session=session,
            emit=emit,
            permission_resolver=permission_resolver,
            track_background=self._track_background_task,
        )
        prompt = review_prompt(self.store, session, steps=result.steps)

        async def run() -> None:
            try:
                await review.start(prompt)
            except asyncio.CancelledError:
                raise
            except Exception:
                return None

        task = asyncio.create_task(run())
        self._track_background_task(task)

    async def abort(self, session_id: str) -> bool:
        with self._operation_lock:
            cleared = self._queues.pop(session_id, None)
            self._clear_queue_block(session_id)
            task = self._tasks.get(session_id)
            active = task is not None and not task.done()
            if task is not None and active:
                self._aborting.add(session_id)

                # Give a just-created task its first tick so its cancellation
                # handler can publish the terminal event.
                def request_cancel() -> None:
                    if not task.done() and not task.cancelling():
                        task.cancel()

                asyncio.get_running_loop().call_soon(request_cancel)
            else:
                self._aborting.discard(session_id)
        if cleared:
            await self.publish_queue(session_id)
            self._maybe_start_background(session_id)
        return active

    # ------------------------------------------------------------------
    # Permissions
    # ------------------------------------------------------------------
    async def _resolve_permission(
        self, request: PermissionRequest, request_id: str, *, session_id: str | None = None
    ) -> Approval:
        future: asyncio.Future[Approval] = asyncio.get_running_loop().create_future()
        self._waiters[request_id] = future
        if session_id is not None:
            self._waiter_sessions[request_id] = session_id
        try:
            return await asyncio.wait_for(future, timeout=PERMISSION_TIMEOUT_S)
        except TimeoutError:
            return Approval(decision="deny", reason="permission request timed out")
        finally:
            self._waiters.pop(request_id, None)
            self._waiter_sessions.pop(request_id, None)

    def resolve_permission(
        self,
        request_id: str,
        decision: str,
        reason: str | None = None,
        *,
        session_id: str | None = None,
    ) -> bool:
        if session_id is not None and self._waiter_sessions.get(request_id) != session_id:
            return False
        future = self._waiters.get(request_id)
        if future is None or future.done():
            return False
        if decision not in {"once", "session", "always", "deny"}:
            return False
        future.set_result(Approval(decision=decision, reason=reason))  # type: ignore[arg-type]
        return True

    async def _ask_question(
        self,
        session_id: str,
        question: Question,
        tool_call_id: str | None,
        emit: EventEmitter,
    ) -> str | None:
        request = QuestionRequest(
            request_id=f"qst_{uuid.uuid4().hex}",
            tool_call_id=tool_call_id,
            **question.model_dump(),
        )
        future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        entry = (request, future)
        self._questions[session_id] = entry
        reason = "cancelled"
        try:
            await emit("question.request", request.model_dump(mode="json"))
            answer = await asyncio.wait_for(future, timeout=QUESTION_TIMEOUT_S)
            reason = "answered" if answer is not None else "dismissed"
            return answer
        except TimeoutError:
            reason = "timeout"
            return None
        finally:
            if self._questions.get(session_id) is entry:
                self._questions.pop(session_id, None)
            await emit("question.resolved", {"request_id": request.request_id, "reason": reason})

    def pending_question(self, session_id: str) -> QuestionRequest | None:
        pending = self._questions.get(session_id)
        return pending[0] if pending and not pending[1].done() else None

    def answer_question(self, session_id: str, request_id: str, answer: str | None) -> bool:
        if session_id in self._aborting:
            return False
        pending = self._questions.get(session_id)
        if pending is None:
            return False
        request, future = pending
        if request.request_id != request_id or future.done():
            return False
        future.set_result(request.validate_answer(answer) if answer is not None else None)
        return True

    # ------------------------------------------------------------------
    # Models and lifecycle
    # ------------------------------------------------------------------
    async def list_models(self, provider: str) -> list[ModelInfo]:
        return await self.registry.list_models(provider)

    def has_credential(self, provider: str) -> bool:
        config = self.config.providers.get(provider)
        if config is None:
            return False
        if config.auth.scheme == "none":
            return True
        if supports_chatgpt_login(config):
            return self._chatgpt_connected()
        if config.auth.env and os.environ.get(config.auth.env):
            return True
        return self.auth.configured(provider)

    def _chatgpt_connected(self) -> bool:
        try:
            return self.registry.chatgpt_auth.connected()
        except AuthError:
            return False

    async def reload_config(self, config: Config) -> None:
        """Apply a newly written config without restarting the daemon."""

        old_registry = self.registry
        additive = all(
            config.providers.get(name) == preset for name, preset in self.config.providers.items()
        )
        self.config = config
        if additive:
            old_registry.config = config
        else:
            self.registry = ProviderRegistry(
                config, self.auth, chatgpt_auth=old_registry.chatgpt_auth
            )
        self.router = RoleRouter(config, self.registry)
        self.loop.config = config
        self.loop.router = self.router
        if not additive:
            await old_registry.aclose()

    # ------------------------------------------------------------------
    # TUI support
    # ------------------------------------------------------------------
    def messages(self, session_id: str) -> list[dict[str, Any]]:
        return self.store.get_message_views(session_id)

    async def mcp_status(self, *, refresh: bool = True) -> list[dict[str, Any]]:
        return await self.loop.mcp_status(refresh=refresh)

    async def restart_mcp(self, name: str) -> dict[str, Any]:
        return await self.loop.restart_mcp(name)

    def usage(self, session: Session) -> dict[str, Any]:
        totals = self.store.usage_totals(session.id)
        context_window = self._context_window(session.model)
        # OpenAI-style prompt/input tokens already include the cache-read subset.
        provider, _, model = (session.model or "").partition("/")
        context_tokens = (
            self.store.last_context_input(session.id, provider, model)
            if provider and model
            else None
        )
        context_percent = None
        if context_window and context_tokens is not None:
            context_percent = round(min(100.0, (context_tokens / context_window) * 100.0), 2)
        return {
            **totals.model_dump(mode="json"),
            "context_window": context_window,
            "context_tokens": context_tokens,
            "context_percent": context_percent,
        }

    def _context_window(self, model: str | None) -> int | None:
        if not model:
            return None
        provider, _, model_id = model.partition("/")
        if not provider or not model_id:
            return None
        catalog_model = self.registry.cached_model(provider, model_id)
        if catalog_model is not None and catalog_model.context_window:
            return catalog_model.context_window
        try:
            return load_profile(provider, model_id).context_window
        except (OSError, ValueError):
            return None

    def update_session(
        self,
        session_id: str,
        *,
        model: str | None = None,
        mode: str | None = None,
        title: str | None = None,
        effort: str | None = None,
    ) -> Session | None:
        if effort is not None:
            session = self.store.get_session(session_id)
            if session is None:
                return None
            metadata = dict(session.metadata)
            if effort == "off":
                metadata.pop("effort", None)
            else:
                metadata["effort"] = effort
            self.store.update_session(session_id, metadata=metadata)
        self.store.update_session(session_id, model=model, mode=mode, title=title)
        return self.store.get_session(session_id)

    def permission_mode(self) -> str:
        return self.config.permissions.mode

    def set_permission_mode(self, mode: str) -> str:
        if mode not in {"ask", "auto-edit", "yolo"}:
            raise ValueError(f"invalid permission mode: {mode}")
        self.config = self.config.model_copy(
            update={"permissions": self.config.permissions.model_copy(update={"mode": mode})}
        )
        self.loop.config = self.config
        return mode

    def fork_session(
        self,
        session_id: str,
        title: str | None = None,
        checkpoint_id: str | None = None,
    ) -> Session:
        if checkpoint_id:
            with self._exclusive_operation(session_id):
                return self._fork_session(session_id, title, checkpoint_id)
        return self._fork_session(session_id, title, checkpoint_id)

    def _fork_session(
        self, session_id: str, title: str | None, checkpoint_id: str | None
    ) -> Session:
        checkpoint = None
        if checkpoint_id:
            # Validate before creating anything so a rejected fork has no
            # side effects.
            checkpoint = self.store.get_checkpoint(checkpoint_id)
            if checkpoint is None or checkpoint["session_id"] != session_id:
                raise ValueError(f"Checkpoint not found: {checkpoint_id}")
        forked = self.store.fork_session(session_id, title=title)
        if checkpoint is not None:
            manager = CheckpointManager(Path(forked.cwd), session_id, store=self.store)
            try:
                manager.restore(checkpoint["commit_hash"])
            except CheckpointError as exc:
                self.store.delete_session(forked.id)
                raise ValueError(str(exc)) from exc
            self.store.append_trajectory(
                forked.id,
                {
                    "type": "rewind",
                    "checkpoint_id": checkpoint_id,
                    "commit": checkpoint["commit_hash"],
                    "source_session": session_id,
                },
            )
        return forked

    # ------------------------------------------------------------------
    # Checkpoints and context
    # ------------------------------------------------------------------
    def checkpoints(self, session_id: str) -> list[dict[str, Any]]:
        return self.store.list_checkpoints(session_id)

    def changes(
        self,
        session_id: str,
        *,
        scope: str,
        checkpoint_id: str | None = None,
        end_checkpoint_id: str | None = None,
        file_path: str | None = None,
    ) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        if session is None:
            raise ValueError("Session not found")
        workspace = Path(session.cwd)
        policy = PermissionPolicy(workspace, self.config.permissions)
        base = end_id = None

        # Git reads GIT_DIR/GIT_WORK_TREE from the daemon environment and follows
        # a workspace .git file to its target. Review requests must stay inside
        # the session workspace that the permission policy approved.
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "GIT_DIR",
                "GIT_WORK_TREE",
                "GIT_INDEX_FILE",
                "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                "GIT_COMMON_DIR",
            }
        }

        def git(*arguments: str) -> str:
            result = subprocess.run(
                [*prefix, *arguments],
                cwd=workspace,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
            if result.returncode:
                raise ValueError("Changes are unavailable for this workspace or checkpoint.")
            return result.stdout

        if scope == "turn":
            # Oldest-first snapshots. A new initial checkpoint marks a new turn.
            records = self.store.list_checkpoints(session_id, limit=10_000)
            anchors = [
                index
                for index, row in enumerate(records)
                if row["label"] == "initial"
                and (checkpoint_id is None or checkpoint_id == row["id"])
            ]
            if not anchors:
                return {"scope": scope, "files": [], "available": False, "truncated": False}
            anchor = anchors[-1]
            next_turn = next(
                (i for i in range(anchor + 1, len(records)) if records[i]["label"] == "initial"),
                len(records),
            )
            start = records[anchor]
            end = next(
                (
                    row
                    for row in reversed(records[anchor + 1 : next_turn])
                    if str(row["label"]).split(":", 1)[0]
                    in {"write_file", "edit_file", "bash", "task"}
                ),
                start,
            )
            if end_checkpoint_id is not None:
                pinned = next(
                    (row for row in records[anchor:next_turn] if row["id"] == end_checkpoint_id),
                    None,
                )
                if pinned is None:
                    raise ValueError("The requested snapshot is not in this turn")
                end = pinned
            base = start["id"]
            end_id = end["id"]
            manager = CheckpointManager(workspace, session_id, store=self.store)
            if not manager.git_dir.is_dir():
                return {"scope": scope, "files": [], "available": False, "truncated": False}
            prefix = ["git", "--git-dir", str(manager.git_dir), "--work-tree", str(workspace)]
            refs = [str(start["commit_hash"]), str(end["commit_hash"])]
            untracked = []
        else:
            prefix = ["git"]
            resolved = workspace.resolve()
            top = Path(git("rev-parse", "--show-toplevel").strip()).resolve()
            if top != resolved and top not in resolved.parents:
                raise ValueError("Changes are unavailable for this workspace or checkpoint.")
            # A .git file may point at any repository. Accept a normal checkout,
            # a linked worktree whose back-pointer names this workspace, or a
            # submodule owned by an ancestor repository. Use the worktree root
            # so a session opened in a repository subdirectory still works.
            git_dir = Path(git("rev-parse", "--absolute-git-dir").strip()).resolve()
            root = top
            dot_git = (root / ".git").resolve()
            owned = git_dir == dot_git or root in git_dir.parents
            if not owned and (git_dir / "gitdir").is_file():
                try:
                    linked = Path(
                        (git_dir / "gitdir").read_text(encoding="utf-8").strip()
                    ).resolve()
                except OSError:
                    linked = None
                owned = linked == dot_git
            if (
                not owned
                and git_dir.parent.name == "modules"
                and git_dir.parent.parent.name == ".git"
            ):
                superproject = git_dir.parent.parent.parent
                owned = superproject == root or superproject in root.parents
            if not owned:
                raise ValueError("Changes are unavailable for this workspace or checkpoint.")
            head = subprocess.run(
                [*prefix, "rev-parse", "--verify", "--quiet", "HEAD"],
                cwd=workspace,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            # An unborn branch has no HEAD; diff against the empty tree so
            # staged and untracked files still appear in the review.
            refs = (
                ["HEAD"]
                if head.returncode == 0
                else [git("hash-object", "-t", "tree", "--stdin").strip()]
            )
            untracked = git("ls-files", "--others", "--exclude-standard", "-z").split("\0")[:-1]
        diff = ["diff", "--no-ext-diff", "--no-textconv", "--find-renames", "--relative"]
        entries = iter(git(*diff, "--name-status", "-z", *refs, "--").split("\0")[:-1])
        paths: list[tuple[str, str, str | None]] = []
        for status in entries:
            first = next(entries)
            if status.startswith(("R", "C")):
                paths.append((status[0], next(entries), first))
            else:
                paths.append((status[0], first, None))
        paths.extend(("?", path, None) for path in untracked)
        files = []
        for status, path, old_path in paths[:100]:
            if file_path is not None and path != file_path:
                continue
            actual_paths = [path, old_path] if old_path else [path]
            if any(
                policy.check(
                    PermissionRequest(
                        tool="read_file",
                        kind="read",
                        path=str(workspace / name),
                        preview="Review changes",
                    )
                )
                != "allow"
                for name in actual_paths
            ):
                continue
            patch = (
                ""
                if status == "?" or file_path is None
                else git(*diff, *refs, "--", *(f":(literal){name}" for name in actual_paths))
            )
            files.append(
                {
                    "path": path,
                    "old_path": old_path,
                    "status": status,
                    "patch": redact_secrets(patch)[:24_000],
                    "truncated": len(patch) > 24_000,
                }
            )
        return {
            "scope": scope,
            "files": files,
            "base": base,
            "end": end_id,
            "available": True,
            "truncated": len(paths) > 100,
        }

    def create_checkpoint(self, session_id: str, label: str | None = None) -> dict[str, Any]:
        with self._exclusive_operation(session_id):
            return self._create_checkpoint(session_id, label)

    def _create_checkpoint(self, session_id: str, label: str | None) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")
        manager = CheckpointManager(Path(session.cwd), session.id, store=self.store)
        checkpoint_id = manager.snapshot(label or "manual checkpoint", allow_empty=True)
        if checkpoint_id is None:
            raise ValueError("Checkpoint could not be created")
        checkpoint = self.store.get_checkpoint(checkpoint_id)
        if checkpoint is None:
            raise ValueError("Checkpoint metadata is missing")
        return checkpoint

    def rewind(
        self,
        session_id: str,
        *,
        checkpoint_id: str | None = None,
        steps: int = 1,
        confirm: bool = False,
    ) -> dict[str, Any]:
        with self._exclusive_operation(session_id):
            return self._rewind(
                session_id, checkpoint_id=checkpoint_id, steps=steps, confirm=confirm
            )

    def _rewind(
        self, session_id: str, *, checkpoint_id: str | None, steps: int, confirm: bool
    ) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")
        checkpoints = self.store.list_checkpoints(session_id)
        if checkpoint_id:
            checkpoint = self.store.get_checkpoint(checkpoint_id)
            if checkpoint is None or checkpoint["session_id"] != session_id:
                raise ValueError(f"Checkpoint not found: {checkpoint_id}")
        else:
            index = len(checkpoints) - 1 - max(steps, 1)
            if index < 0:
                raise ValueError("No previous checkpoint is available")
            checkpoint = checkpoints[index]
        if not confirm:
            raise ConfirmationRequired(checkpoint)
        manager = CheckpointManager(Path(session.cwd), session.id, store=self.store)
        try:
            manager.restore(checkpoint["commit_hash"])
        except CheckpointError as exc:
            raise ValueError(str(exc)) from exc
        self.store.append_trajectory(
            session_id,
            {
                "type": "rewind",
                "checkpoint_id": checkpoint["id"],
                "commit": checkpoint["commit_hash"],
            },
        )
        return checkpoint

    async def compact_session(self, session_id: str) -> dict[str, Any]:
        with self._exclusive_operation(session_id):
            return await self._compact_session(session_id)

    async def _compact_session(self, session_id: str) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")
        through = self.store.max_message_seq(session_id)
        previous = str(session.metadata.get("context_summary", ""))
        messages = self.store.get_context_messages(session_id)
        if previous and messages and messages[0].role == "system":
            messages = messages[1:]
        todo_text = "\n".join(
            f"[{item.get('status', 'pending')}] {item.get('content', '')}"
            for item in (session.metadata or {}).get("todos", [])
            if isinstance(item, dict)
        )
        before = sum(estimate_tokens(message.content or "") for message in messages)
        if previous:
            before += estimate_tokens(previous)
        summary = fallback_summary(messages, todo_text)
        if previous:
            summary = previous + "\n\n" + summary
        usage = Usage()
        if self.config.roles.fast:
            try:

                async def emit(_event) -> None:
                    return None

                outcome = await self.router.stream(
                    "fast",
                    messages=compaction_prompt(messages, todo_text, previous_summary=previous),
                    tools=[],
                    session_id=session_id,
                    emit=emit,
                    temperature=0.0,
                    max_tokens=1200,
                )
                usage = outcome.usage
                if outcome.text and outcome.text.strip():
                    summary = outcome.text.strip()
            except (ConfigError, ProviderError):
                # A failed fast-role call falls back to the deterministic summary.
                pass
        summary = summary[: self.config.context.summary_max_chars]
        self.store.set_context_summary(session_id, summary, through)
        if usage.input_tokens or usage.output_tokens or usage.allowance:
            self.store.record_usage(
                session_id=session_id,
                step_id="compact",
                provider=None,
                model=session.model,
                usage=usage,
            )
        after = estimate_tokens(summary)
        return {
            "summary": summary,
            "through_seq": through,
            "before_tokens": before,
            "after_tokens": after,
        }

    def plan(self, session_id: str) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")
        path = Path(session.cwd) / ".siwah" / "plan.md"
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            content = ""
        return {"path": str(path), "content": content, "exists": bool(content)}

    def tool_output(self, session_id: str, tool_call_id: str) -> dict[str, Any]:
        record = self.store.get_tool_call(tool_call_id)
        if record is None or record["session_id"] != session_id:
            raise ValueError(f"Tool call not found: {tool_call_id}")
        result = record.get("result") or {}
        data = result.get("data") if isinstance(result, dict) else {}
        if isinstance(data, dict) and isinstance(data.get("full_output"), str):
            output = data["full_output"]
        elif isinstance(result, dict):
            output = str(result.get("output", ""))
        else:
            output = ""
        return {
            "tool_call_id": tool_call_id,
            "name": record["name"],
            "output": output,
            "handle": data.get("handle") if isinstance(data, dict) else None,
        }

    def skills(self, workspace: Path | None = None) -> list[Any]:
        return list_skills(workspace)

    def skill(self, name: str, workspace: Path | None = None) -> Any:
        return read_skill(name, workspace)

    def memory(self, cwd: str | None = None) -> Any:
        workspace = Path(cwd or Path.cwd()).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError(f"Workspace does not exist: {workspace}")
        return read_memory(workspace)

    def learning(self, cwd: str | None = None) -> dict[str, Any]:
        workspace = Path(cwd or Path.cwd()).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError(f"Workspace does not exist: {workspace}")
        usage = load_usage()
        state = load_state()
        return {
            "skills": [
                {
                    "name": skill.name,
                    "description": skill.description,
                    "state": str(usage.get(skill.name, {}).get("state") or "active"),
                    "use_count": int(usage.get(skill.name, {}).get("use_count", 0)),
                    "created_by": str(usage.get(skill.name, {}).get("created_by") or "unknown"),
                }
                for skill in list_skills(workspace, include_archived=True)
            ],
            "lessons": [
                {
                    "cue": lesson.cue,
                    "lesson": lesson.lesson,
                    "confidence": lesson.confidence,
                    "source": lesson.source,
                    "created": lesson.created,
                }
                for lesson in read_lessons(workspace)
            ],
            "evolution_tag": state.tag,
            "evolution_status": state.status,
        }

    def init_memory(self, cwd: str) -> Any:
        workspace = Path(cwd).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError(f"Workspace does not exist: {workspace}")
        init_project_memory(workspace)
        return init_agents_file(workspace)

    def write_quick_note(self, content: str) -> Any:
        return quick_note(content)

    async def files(self, session_id: str, limit: int = 500) -> list[str]:
        session = self.store.get_session(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")
        return await self.files_for_cwd(session.cwd, limit)

    async def files_for_cwd(self, cwd: str, limit: int = 500) -> list[str]:
        return await asyncio.to_thread(_list_files, Path(cwd), limit)

    def set_default_effort(self, effort: str | None) -> str | None:
        normalized = effort.strip().lower() if effort else None
        if normalized in {"", "default", "none", "off"}:
            normalized = None
        try:
            update_agent_effort(normalized)
        except (OSError, ValueError) as exc:
            raise ValueError(f"Could not save effort: {exc}") from exc
        self.config = self.config.model_copy(
            update={"agent": self.config.agent.model_copy(update={"effort": normalized})}
        )
        self.loop.config = self.config
        return normalized

    def auth_status(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for name, provider in self.config.providers.items():
            env_name = provider.auth.env
            chatgpt = supports_chatgpt_login(provider)
            openrouter = supports_browser_login(provider)
            result.append(
                {
                    "provider": name,
                    "scheme": provider.auth.scheme,
                    "env": env_name,
                    "env_set": bool(env_name and os.environ.get(env_name)),
                    "file_set": self._chatgpt_connected()
                    if chatgpt
                    else self.auth.configured(name),
                    "browser_login": chatgpt or openrouter,
                    "browser_method": "chatgpt"
                    if chatgpt
                    else "openrouter"
                    if openrouter
                    else None,
                }
            )
        return result

    def auth_login(self, provider: str, api_key: str) -> None:
        if provider not in self.config.providers:
            raise ValueError(f"Unknown provider: {provider}")
        if supports_chatgpt_login(self.config.providers[provider]):
            raise ValueError("Sign in with ChatGPT in your browser.")
        self.auth.save_key(provider, api_key)

    async def run_shell(
        self, session_id: str, command: str, *, approved: bool = False, timeout_s: float = 120.0
    ) -> dict[str, Any]:
        with self._exclusive_operation(session_id):
            return await self._run_shell(
                session_id, command, approved=approved, timeout_s=timeout_s
            )

    async def _run_shell(
        self, session_id: str, command: str, *, approved: bool, timeout_s: float
    ) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")
        workspace = Path(session.cwd).resolve()
        policy = PermissionPolicy(workspace, self.config.permissions)
        request = PermissionRequest(
            tool="bash",
            kind="bash",
            command=command,
            preview=f"$ {command}",
        )
        decision = policy.check(request)
        if decision == "deny":
            raise ShellDenied("Command denied by policy")
        if decision == "ask" and not approved:
            raise ShellPermissionRequired(request.preview)

        async def approver(_request: PermissionRequest) -> Approval:
            return Approval(decision="once")

        context = ToolContext(
            workspace=workspace,
            policy=policy,
            session_id=session_id,
            agent_config=self.config.agent,
            approver=approver,
            sandbox=Sandbox(self.config.sandbox, cwd=workspace)
            if self.config.sandbox.enabled
            else None,
        )
        result = await BashTool().run(context, command=command, timeout_s=timeout_s)
        return {
            "ok": result.ok,
            "output": result.output,
            "exit_code": (result.data or {}).get("exit_code") if result.ok else None,
        }

    async def start_cron(self) -> None:
        """Start the opt-in local scheduler after the event loop is running."""

        await self.cron.start()

    async def close(self) -> None:
        with self._operation_lock:
            self._closing = True
            tasks = [*self._tasks.values(), *self._background_tasks]
            for retry in self._queue_retries.values():
                retry.cancel()
            self._queue_retries.clear()
            for retry in self._background_retries.values():
                retry.cancel()
            self._background_retries.clear()
            self._background_pending.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self.cron.stop()
        await self.loop.close_jobs()
        await self.registry.aclose()
        self.store.close()


class SessionBusy(RuntimeError):
    """A workspace mutation conflicts with a run or another reserved operation."""


def _overlaps(left: Path, right: Path) -> bool:
    return left.is_relative_to(right) or right.is_relative_to(left)


def _list_files(root: Path, limit: int) -> list[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "-c", "core.quotepath=false", "ls-files", "-z"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if completed.returncode == 0:
            decoded = completed.stdout.decode("utf-8", errors="replace")
            files = [name for name in decoded.split("\0") if name]
            if files:
                return files[:limit]
    except (OSError, subprocess.SubprocessError):
        pass
    files: list[str] = []
    skip = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__"}
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in skip]
        for name in filenames:
            path = Path(directory) / name
            files.append(str(path.relative_to(root)).replace("\\", "/"))
            if len(files) >= limit:
                return files
    return files


__all__ = [
    "ConfigError",
    "DaemonRuntime",
    "ShellDenied",
    "ShellPermissionRequired",
]
