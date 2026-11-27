"""Small, local, user-editable model of durable user preferences."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .memory import memory_dir, secret_marker

MAX_FIELD = 500
MAX_LIST = 20
MAX_CONTEXT = 3_000
FIELDS = {
    "language": str,
    "communication": str,
    "stack": list,
    "testing": list,
    "risk_tolerance": str,
    "preferences": list,
    "notes": str,
}


def profile_path() -> Path:
    return memory_dir() / "profile.json"


def _default() -> dict[str, Any]:
    return {
        "language": "",
        "communication": "",
        "stack": [],
        "testing": [],
        "risk_tolerance": "",
        "preferences": [],
        "notes": "",
        "updated_at": "",
    }


def _clean(value: Any, expected: type) -> Any:
    if expected is str:
        text = str(value or "").strip()
        if len(text) > MAX_FIELD:
            raise ValueError(f"profile field exceeds {MAX_FIELD} characters")
        return text
    if expected is list:
        if isinstance(value, str):
            value = [item.strip() for item in value.split(",") if item.strip()]
        if not isinstance(value, list):
            raise ValueError("profile list fields require a list or comma-separated string")
        items = [str(item).strip() for item in value if str(item).strip()]
        if len(items) > MAX_LIST:
            raise ValueError(f"profile list exceeds {MAX_LIST} items")
        return items[:MAX_LIST]
    raise ValueError("unsupported profile field type")


def read_profile() -> dict[str, Any]:
    data = _default()
    try:
        payload = json.loads(profile_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return data
    if isinstance(payload, dict):
        for key in FIELDS:
            if key in payload:
                data[key] = payload[key]
        data["updated_at"] = str(payload.get("updated_at") or "")
    return data


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        with contextlib.suppress(OSError):
            temp.chmod(0o600)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def update_profile(**updates: Any) -> dict[str, Any]:
    data = read_profile()
    changed = False
    for key, expected in FIELDS.items():
        if key not in updates:
            continue
        value = _clean(updates[key], expected)
        marker = secret_marker(str(value))
        if marker:
            raise ValueError(f"refusing to store a {marker}")
        if data.get(key) != value:
            data[key] = value
            changed = True
    if changed:
        from datetime import UTC, datetime

        data["updated_at"] = datetime.now(UTC).isoformat()
        _atomic_write(profile_path(), data)
    return data


def profile_context() -> str:
    data = read_profile()
    lines: list[str] = []
    for label, key in (
        ("Language", "language"),
        ("Communication", "communication"),
        ("Stack", "stack"),
        ("Testing", "testing"),
        ("Risk tolerance", "risk_tolerance"),
        ("Preferences", "preferences"),
        ("Notes", "notes"),
    ):
        value = data.get(key)
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value)
        if value:
            lines.append(f"- {label}: {value}")
    if not lines:
        return ""
    text = "## User profile\n" + "\n".join(lines)
    if len(text) > MAX_CONTEXT:
        text = text[:MAX_CONTEXT].rstrip() + "\n... profile truncated"
    return text


__all__ = ["FIELDS", "profile_context", "profile_path", "read_profile", "update_profile"]
