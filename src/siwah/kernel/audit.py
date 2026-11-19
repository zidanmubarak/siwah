"""Append-only, hash-chained audit log for kernel and supervisor events."""

from __future__ import annotations

import contextlib
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .paths import siwah_home
from .secrets import redact_secrets


def audit_path() -> Path:
    return siwah_home() / "audit.jsonl"


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_value(item) for key, item in value.items()}
    return value


def _last_hash(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        value = record.get("hash")
        if isinstance(value, str):
            return value
    return ""


def audit_event(
    event: str, data: dict[str, Any] | None = None, *, path: Path | None = None
) -> dict[str, Any]:
    """Append one event and return the record that was written."""

    target = path or audit_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        "data": _redact_value(data or {}),
        "prev": _last_hash(target),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    payload["hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    line = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    with target.open("a", encoding="utf-8") as handle:
        handle.write(line)
    with contextlib.suppress(OSError):
        target.chmod(0o600)
    return payload


def read_events(*, path: Path | None = None, limit: int = 50) -> list[dict[str, Any]]:
    target = path or audit_path()
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            events.append(record)
    return events[-limit:]


def verify_chain(*, path: Path | None = None) -> bool:
    target = path or audit_path()
    previous = ""
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return True
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return False
        if not isinstance(record, dict) or record.get("prev") != previous:
            return False
        claimed = record.get("hash")
        payload = {key: value for key, value in record.items() if key != "hash"}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        actual = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if claimed != actual:
            return False
        previous = actual
    return True


__all__ = ["audit_event", "audit_path", "read_events", "verify_chain"]
