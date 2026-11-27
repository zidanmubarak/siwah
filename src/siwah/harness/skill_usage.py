"""Local skill usage telemetry and conservative lifecycle curation."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from filelock import FileLock

from ..paths import harness_dir

USAGE_NAME = ".usage.json"
LOCK_NAME = ".usage.lock"
CURATOR_NAME = ".curator.json"
STATE_ACTIVE = "active"
STATE_STALE = "stale"
STATE_ARCHIVED = "archived"
STATES = {STATE_ACTIVE, STATE_STALE, STATE_ARCHIVED}


def _skills_dir() -> Path:
    path = harness_dir() / "skills"
    path.mkdir(parents=True, exist_ok=True)
    return path


def usage_path() -> Path:
    return _skills_dir() / USAGE_NAME


def curator_path() -> Path:
    return _skills_dir() / CURATOR_NAME


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        with contextlib.suppress(OSError):
            temp.chmod(0o600)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def load_usage() -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(usage_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    skills = payload.get("skills") if isinstance(payload, dict) else None
    if not isinstance(skills, dict):
        return {}
    return {str(name): dict(record) for name, record in skills.items() if isinstance(record, dict)}


def _save_usage(skills: dict[str, dict[str, Any]]) -> None:
    _atomic_write(usage_path(), {"version": 1, "skills": skills})


def _mutate(name: str, callback) -> dict[str, Any]:
    lock = FileLock(str(usage_path().with_name(LOCK_NAME)), timeout=5)
    with lock:
        skills = load_usage()
        record = dict(skills.get(name, {}))
        callback(record)
        skills[name] = record
        _save_usage(skills)
        return record


def record_event(
    name: str,
    event: str,
    *,
    created_by: str | None = None,
    state: str | None = None,
) -> dict[str, Any]:
    """Record a view/patch/create/delete event for one skill."""

    if not name:
        raise ValueError("skill name is required")
    if event not in {"create", "view", "patch", "delete", "pin", "unpin"}:
        raise ValueError(f"unknown skill usage event: {event}")

    def apply(record: dict[str, Any]) -> None:
        now = _now()
        record.setdefault("created_at", now)
        if event == "create":
            record["created_by"] = created_by or "agent"
            record["state"] = STATE_ACTIVE
        elif event == "view":
            record["view_count"] = int(record.get("view_count", 0)) + 1
            record["use_count"] = int(record.get("use_count", 0)) + 1
            record["last_used_at"] = now
            if record.get("state") in {STATE_STALE, STATE_ARCHIVED}:
                record["state"] = STATE_ACTIVE
        elif event == "patch":
            record["patch_count"] = int(record.get("patch_count", 0)) + 1
            record["last_patched_at"] = now
        elif event == "pin":
            record["pinned"] = True
        elif event == "unpin":
            record["pinned"] = False
        else:
            record.setdefault("created_by", "unknown")
        if state is not None:
            if state not in STATES:
                raise ValueError(f"unknown skill state: {state}")
            record["state"] = state
        record.setdefault("state", STATE_ACTIVE)
        record.setdefault("pinned", False)

    if event == "delete":
        lock = FileLock(str(usage_path().with_name(LOCK_NAME)), timeout=5)
        with lock:
            skills = load_usage()
            skills.pop(name, None)
            _save_usage(skills)
        return {}
    return _mutate(name, apply)


def set_state(name: str, state: str) -> dict[str, Any]:
    if state not in STATES:
        raise ValueError(f"unknown skill state: {state}")
    return _mutate(name, lambda record: record.update(state=state))


def set_pinned(name: str, pinned: bool) -> dict[str, Any]:
    return _mutate(name, lambda record: record.update(pinned=pinned))


def state_for(name: str) -> str:
    return str(load_usage().get(name, {}).get("state") or STATE_ACTIVE)


def archived_names() -> set[str]:
    return {name for name, record in load_usage().items() if record.get("state") == STATE_ARCHIVED}


def _timestamp(record: dict[str, Any]) -> datetime | None:
    for key in ("last_used_at", "last_patched_at", "created_at"):
        value = record.get(key)
        if not isinstance(value, str):
            continue
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            continue
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _curator_state() -> dict[str, Any]:
    try:
        payload = json.loads(curator_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def maybe_curate(
    *,
    interval_hours: int = 24,
    stale_after_days: int = 14,
    archive_after_days: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run deterministic curation at most once per interval after a session."""

    if os.environ.get("SIWAH_CURATOR", "1") == "0":
        return {"status": "disabled"}
    current = now or datetime.now(UTC)
    state = _curator_state()
    previous = state.get("last_run_at")
    if isinstance(previous, str):
        try:
            last = datetime.fromisoformat(previous)
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            if current - last < timedelta(hours=max(1, interval_hours)):
                return {"status": "not_due", "last_run_at": previous}
        except ValueError:
            pass
    result = curate(
        stale_after_days=stale_after_days,
        archive_after_days=archive_after_days,
        now=current,
    )
    _atomic_write(
        curator_path(),
        {
            "last_run_at": current.isoformat(),
            "last_summary": result,
        },
    )
    return {"status": "ran", **result}


def curate(
    *,
    stale_after_days: int = 14,
    archive_after_days: int = 30,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Move agent-created, unpinned skills to stale/archived by inactivity."""

    if stale_after_days < 1 or archive_after_days <= stale_after_days:
        raise ValueError("archive_after_days must be greater than stale_after_days >= 1")
    current = now or datetime.now(UTC)
    stale_cutoff = current - timedelta(days=stale_after_days)
    archive_cutoff = current - timedelta(days=archive_after_days)
    skills = load_usage()
    changed: list[dict[str, str]] = []
    counts = {"checked": 0, "marked_stale": 0, "archived": 0, "skipped": 0}
    for name, record in skills.items():
        counts["checked"] += 1
        if record.get("created_by") != "agent" or record.get("pinned"):
            counts["skipped"] += 1
            continue
        anchor = _timestamp(record)
        if anchor is None:
            counts["skipped"] += 1
            continue
        state = str(record.get("state") or STATE_ACTIVE)
        target = state
        if anchor <= archive_cutoff:
            target = STATE_ARCHIVED
        elif anchor <= stale_cutoff and state == STATE_ACTIVE:
            target = STATE_STALE
        if target == state:
            continue
        record["state"] = target
        counts["archived" if target == STATE_ARCHIVED else "marked_stale"] += 1
        changed.append({"name": name, "from": state, "to": target})
    if changed and not dry_run:
        _save_usage(skills)
    return {"dry_run": dry_run, **counts, "changed": changed}


__all__ = [
    "archived_names",
    "curate",
    "curator_path",
    "load_usage",
    "maybe_curate",
    "record_event",
    "set_pinned",
    "set_state",
    "state_for",
    "usage_path",
]
