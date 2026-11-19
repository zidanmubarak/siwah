"""Restart hook for Level 2 promotions."""

from __future__ import annotations

import contextlib
import json
import os
import signal
from collections.abc import Callable

from .paths import runtime_path


class RestartController:
    def __call__(self) -> bool:
        try:
            payload = json.loads(runtime_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return True
        pid = payload.get("pid") if isinstance(payload, dict) else None
        if not isinstance(pid, int):
            return True
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
        return True


Restart = Callable[[], bool]


__all__ = ["Restart", "RestartController"]
