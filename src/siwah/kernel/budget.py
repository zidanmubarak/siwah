"""Budget governor for evolution cycles and optional weekly allowance."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .paths import siwah_home, write_private
from .policy import BudgetConfig


class BudgetState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    date: str = ""
    cycles_today: int = 0
    allowance_today: float = 0.0
    week_start: str = ""
    weekly_spent: float = 0.0
    corrupt: bool = False
    total_cycles: int = 0
    total_allowance: float = 0.0
    last_duration_s: float = 0.0
    last_cycle_at: str | None = None


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    reason: str | None = None


class BudgetGovernor:
    """Enforce cycle, cooldown, allowance, and reserve limits.

    The weekly allowance is optional because the plan limit is a provider
    fact. When supplied, the governor stops before a cycle would consume the
    reserved share.
    """

    def __init__(
        self,
        config: BudgetConfig,
        *,
        state_path: Path | str | None = None,
        weekly_allowance: float | None = None,
    ) -> None:
        self.config = config
        self.state_path = (
            Path(state_path).expanduser().resolve()
            if state_path is not None
            else siwah_home() / "budget-state.json"
        )
        self.weekly_allowance = weekly_allowance

    def load_state(self, *, now: datetime | None = None) -> BudgetState:
        current = now or datetime.now(UTC)
        state = self._read()
        if state.date != current.date().isoformat():
            state = state.model_copy(
                update={
                    "date": current.date().isoformat(),
                    "cycles_today": 0,
                    "allowance_today": 0.0,
                }
            )
        week_start: datetime | None = None
        if state.week_start:
            try:
                week_start = datetime.fromisoformat(state.week_start)
            except ValueError:
                week_start = None
        if week_start is None or current - week_start >= timedelta(days=7):
            # The provider allowance window rolls; a lifetime accumulator would
            # eventually block evolution forever.
            state = state.model_copy(
                update={"week_start": current.isoformat(), "weekly_spent": 0.0}
            )
        return state

    def _read(self) -> BudgetState:
        try:
            return BudgetState.model_validate_json(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return BudgetState()
        except (OSError, ValueError):
            # A reset would silently hand back the full budget; fail closed and
            # let a human inspect the damaged state file.
            return BudgetState(corrupt=True)

    def _write(self, state: BudgetState) -> None:
        write_private(self.state_path, json.dumps(state.model_dump(mode="json"), indent=2) + "\n")

    def can_start(
        self,
        *,
        now: datetime | None = None,
        estimated_allowance: float | None = None,
    ) -> BudgetDecision:
        current = now or datetime.now(UTC)
        state = self.load_state(now=current)
        if state.corrupt:
            return BudgetDecision(False, "budget state is unreadable")
        if state.cycles_today >= self.config.max_cycles_per_day:
            return BudgetDecision(False, "max_cycles_per_day reached")
        if state.last_cycle_at:
            try:
                last = datetime.fromisoformat(state.last_cycle_at)
            except ValueError:
                last = None
            if last is not None:
                elapsed_minutes = (current - last).total_seconds() / 60.0
                if elapsed_minutes < self.config.cooldown_minutes:
                    return BudgetDecision(False, "cooldown active")
        if self.weekly_allowance is not None and self.weekly_allowance > 0:
            reserve = self.weekly_allowance * (self.config.reserve_weekly_pct / 100.0)
            estimate = (
                estimated_allowance
                if estimated_allowance is not None
                else self.config.max_allowance_per_cycle
            )
            remaining = self.weekly_allowance - state.weekly_spent
            if remaining - estimate < reserve:
                return BudgetDecision(False, "weekly reserve would be breached")
        return BudgetDecision(True, None)

    def check_duration(self, duration_s: float) -> BudgetDecision:
        limit = self.config.max_wall_minutes * 60.0
        if duration_s > limit:
            return BudgetDecision(False, "max_wall_minutes exceeded")
        return BudgetDecision(True, None)

    def check_cycle(self, allowance: float, duration_s: float) -> BudgetDecision:
        """Check the per-cycle limits before recording a finished cycle."""

        duration = self.check_duration(duration_s)
        if not duration.allowed:
            return duration
        if allowance > self.config.max_allowance_per_cycle:
            return BudgetDecision(False, "max_allowance_per_cycle exceeded")
        return BudgetDecision(True, None)

    def record_cycle(
        self,
        allowance: float,
        duration_s: float,
        *,
        now: datetime | None = None,
    ) -> BudgetState:
        current = now or datetime.now(UTC)
        state = self.load_state(now=current)
        if state.corrupt:
            raise RuntimeError("budget state is unreadable; repair or remove it")
        updated = state.model_copy(
            update={
                "cycles_today": state.cycles_today + 1,
                "allowance_today": state.allowance_today + max(0.0, allowance),
                "weekly_spent": state.weekly_spent + max(0.0, allowance),
                "total_cycles": state.total_cycles + 1,
                "total_allowance": state.total_allowance + max(0.0, allowance),
                "last_duration_s": max(0.0, duration_s),
                "last_cycle_at": current.isoformat(),
            }
        )
        self._write(updated)
        return updated

    def reset_cooldown(self, *, now: datetime | None = None) -> BudgetState:
        """Clear only the cooldown timestamp after a failed preflight.

        Daily cycle counts and recorded allowance stay intact; this is a
        recovery action for a cycle that could not do any useful work.
        """

        current = now or datetime.now(UTC)
        state = self.load_state(now=current)
        if state.corrupt:
            raise RuntimeError("budget state is unreadable; repair or remove it")
        updated = state.model_copy(update={"last_cycle_at": None})
        self._write(updated)
        return updated

    def snapshot(self, *, now: datetime | None = None) -> dict[str, object]:
        current = now or datetime.now(UTC)
        state = self.load_state(now=current)
        remaining = (
            self.weekly_allowance - state.weekly_spent
            if self.weekly_allowance is not None
            else None
        )
        return {
            "state": state.model_dump(mode="json"),
            "config": self.config.model_dump(mode="json"),
            "weekly_allowance": self.weekly_allowance,
            "weekly_remaining": remaining,
        }


__all__ = ["BudgetDecision", "BudgetGovernor", "BudgetState"]
