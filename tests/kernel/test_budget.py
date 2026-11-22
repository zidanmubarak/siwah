from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from siwah.config import BudgetConfig
from siwah.kernel.budget import BudgetGovernor


def _config(**updates) -> BudgetConfig:
    base = {
        "max_allowance_per_cycle": 0.5,
        "max_cycles_per_day": 2,
        "reserve_weekly_pct": 40,
        "max_concurrency": 2,
        "cooldown_minutes": 60,
        "max_wall_minutes": 10,
    }
    base.update(updates)
    return BudgetConfig(**base)


def test_cycle_cooldown_and_daily_reset(tmp_path: Path):
    now = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
    governor = BudgetGovernor(_config(), state_path=tmp_path / "budget.json")

    assert governor.can_start(now=now).allowed
    governor.record_cycle(0.2, 60.0, now=now)
    assert not governor.can_start(now=now).allowed
    assert governor.can_start(now=now + timedelta(minutes=61)).allowed

    governor.record_cycle(0.2, 60.0, now=now + timedelta(minutes=61))
    blocked = governor.can_start(now=now + timedelta(minutes=122))
    assert not blocked.allowed
    assert blocked.reason == "max_cycles_per_day reached"

    next_day = now + timedelta(days=1)
    assert governor.can_start(now=next_day).allowed
    assert governor.load_state(now=next_day).cycles_today == 0
    assert governor.load_state(now=next_day).weekly_spent == 0.4


def test_reset_cooldown_preserves_the_daily_cycle_count(tmp_path: Path):
    now = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
    governor = BudgetGovernor(_config(), state_path=tmp_path / "budget.json")
    governor.record_cycle(0.2, 60.0, now=now)

    assert not governor.can_start(now=now).allowed

    state = governor.reset_cooldown(now=now)

    assert state.last_cycle_at is None
    assert state.cycles_today == 1
    assert governor.can_start(now=now).allowed


def test_weekly_reserve_and_wall_time(tmp_path: Path):
    now = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
    governor = BudgetGovernor(
        _config(),
        state_path=tmp_path / "budget.json",
        weekly_allowance=1.0,
    )
    governor.record_cycle(0.5, 60.0, now=now)

    decision = governor.can_start(now=now + timedelta(minutes=61))
    assert not decision.allowed
    assert decision.reason == "weekly reserve would be breached"
    assert governor.check_duration(600).allowed
    assert not governor.check_duration(601).allowed
    snapshot = governor.snapshot(now=now + timedelta(minutes=61))
    assert snapshot["weekly_remaining"] == 0.5


def test_budget_state_persists(tmp_path: Path):
    now = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
    path = tmp_path / "budget.json"
    governor = BudgetGovernor(_config(), state_path=path)
    governor.record_cycle(0.25, 30.0, now=now)

    restored = BudgetGovernor(_config(), state_path=path)
    state = restored.load_state(now=now)
    assert state.cycles_today == 1
    assert state.allowance_today == 0.25
    assert state.last_duration_s == 30.0
