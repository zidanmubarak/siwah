"""Post-session trigger for automatic evolution cycles."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import EvolutionConfig
from ..core.storage import Store
from ..kernel.audit import audit_event
from ..paths import siwah_home, write_private
from .observer import Observer
from .state import is_paused

Spawn = Callable[[], None]


@dataclass(frozen=True)
class TriggerDecision:
    triggered: bool
    reason: str


def trigger_state_path() -> Path:
    return siwah_home() / "evolution-trigger.json"


class EvolutionTrigger:
    def __init__(
        self,
        config: EvolutionConfig,
        *,
        enabled: bool = True,
        spawn: Spawn | None = None,
        state_path: Path | None = None,
        environ: dict[str, str] | None = None,
        audit: Callable[..., dict[str, Any]] = audit_event,
    ) -> None:
        self.config = config
        self.enabled = enabled
        self.spawn = spawn or self._spawn
        self.state_path = state_path or trigger_state_path()
        self.environ = environ if environ is not None else os.environ
        self.audit = audit

    def after_session(
        self, store: Store, session_id: str, *, now: datetime | None = None
    ) -> TriggerDecision:
        current = now or datetime.now(UTC)
        decision = self.should_trigger(store, session_id, now=current)
        if not decision.triggered:
            return decision
        self.spawn()
        self._write_state(current)
        self.audit(
            "evolve.triggered",
            {"session": session_id, "reason": decision.reason, "at": current.isoformat()},
        )
        return decision

    def should_trigger(
        self, store: Store, session_id: str, *, now: datetime | None = None
    ) -> TriggerDecision:
        current = now or datetime.now(UTC)
        if not self.enabled:
            return TriggerDecision(False, "auto evolution is disabled")
        if self.environ.get("SIWAH_EVOLVE") == "off":
            return TriggerDecision(False, "kill switch is active")
        if self.environ.get("SIWAH_EVOLUTION_CHILD") == "1":
            return TriggerDecision(False, "evolution child process")
        override = self.environ.get("SIWAH_EVOLVE_AUTO")
        if override == "0" or (override != "1" and not self.config.auto):
            return TriggerDecision(False, "auto evolution is off")
        if is_paused():
            return TriggerDecision(False, "evolution is paused")
        previous = self._read_state()
        if previous is not None:
            last = previous.get("last_trigger")
            if isinstance(last, str):
                try:
                    elapsed = current - datetime.fromisoformat(last)
                except ValueError:
                    elapsed = timedelta.max
                if elapsed < timedelta(minutes=self.config.cooldown_minutes):
                    return TriggerDecision(False, "cooldown active")

        observer = Observer()
        observations = observer.observe(store, limit=20)
        current = next((item for item in observations if item.session_id == session_id), None)
        if len(observations) < self.config.min_sessions:
            return TriggerDecision(False, "not enough sessions")
        # Only the session that just finished may justify a cycle; old errors in
        # the window must not keep re-triggering evolution.
        signals = observer.signals([current]) if current is not None else []
        if not signals:
            return TriggerDecision(False, "no evolution signal")
        return TriggerDecision(True, signals[0])

    def _read_state(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write_state(self, now: datetime) -> None:
        write_private(
            self.state_path,
            json.dumps({"last_trigger": now.isoformat()}, indent=2) + "\n",
        )

    def _spawn(self) -> None:
        env = dict(os.environ)
        env["SIWAH_EVOLUTION_CHILD"] = "1"
        env["SIWAH_EVOLVE_AUTO"] = "0"
        subprocess.Popen(
            [sys.executable, "-m", "siwah", "evolve"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
        )


__all__ = ["EvolutionTrigger", "TriggerDecision", "trigger_state_path"]
