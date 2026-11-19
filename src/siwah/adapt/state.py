"""Compatibility exports for protected evolution state."""

from ..kernel.evolution_state import (
    is_paused,
    load_state,
    pause_path,
    save_state,
    set_paused,
    state_path,
)

__all__ = ["is_paused", "load_state", "pause_path", "save_state", "set_paused", "state_path"]
