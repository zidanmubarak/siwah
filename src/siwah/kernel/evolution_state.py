"""Evolution state and pause controls."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .candidates import EvolutionState
from .paths import siwah_home, write_private


def state_path() -> Path:
    return siwah_home() / "evolution.json"


def pause_path() -> Path:
    return siwah_home() / "evolution.paused"


def load_state(*, path: Path | None = None) -> EvolutionState:
    target = path or state_path()
    try:
        return EvolutionState.model_validate_json(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return EvolutionState()


def save_state(state: EvolutionState, *, path: Path | None = None) -> Path:
    target = path or state_path()
    state.updated_at = datetime.now(UTC).isoformat()
    write_private(target, json.dumps(state.model_dump(mode="json"), indent=2) + "\n")
    return target


def is_paused(*, path: Path | None = None) -> bool:
    return (path or pause_path()).exists()


def set_paused(paused: bool, *, path: Path | None = None) -> Path:
    target = path or pause_path()
    if paused:
        write_private(target, datetime.now(UTC).isoformat() + "\n")
    else:
        target.unlink(missing_ok=True)
    return target


__all__ = [
    "is_paused",
    "load_state",
    "pause_path",
    "save_state",
    "set_paused",
    "state_path",
]
