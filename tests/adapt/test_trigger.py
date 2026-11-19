from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from siwah.adapt.state import set_paused
from siwah.adapt.trigger import EvolutionTrigger
from siwah.config import EvolutionConfig
from siwah.core.storage import Store


def _seed_sessions(store: Store, count: int = 3) -> list[str]:
    ids: list[str] = []
    for _index in range(count):
        session = store.create_session(cwd=str(Path.cwd()), model="mock/model-a")
        store.update_session(session.id, status="error")
        ids.append(session.id)
    return ids


def test_trigger_fires_on_error_signal_and_respects_cooldown(store: Store, tmp_path: Path):
    ids = _seed_sessions(store)
    spawned: list[bool] = []
    events: list[tuple[str, dict]] = []
    trigger = EvolutionTrigger(
        EvolutionConfig(auto=True, min_sessions=3, cooldown_minutes=60),
        spawn=lambda: spawned.append(True),
        state_path=tmp_path / "trigger.json",
        environ={},
        audit=lambda event, data: events.append((event, data)) or {},
    )
    now = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)

    first = trigger.after_session(store, ids[0], now=now)
    assert first.triggered
    assert spawned == [True]
    assert any(event == "evolve.triggered" for event, _ in events)

    second = trigger.after_session(store, ids[1], now=now + timedelta(minutes=5))
    assert not second.triggered
    assert second.reason == "cooldown active"
    assert spawned == [True]

    third = trigger.after_session(store, ids[2], now=now + timedelta(minutes=61))
    assert third.triggered
    assert len(spawned) == 2


def test_trigger_respects_kill_switch_pause_and_auto_off(store: Store, tmp_path: Path):
    ids = _seed_sessions(store)
    spawned: list[bool] = []
    trigger = EvolutionTrigger(
        EvolutionConfig(auto=True, min_sessions=3),
        spawn=lambda: spawned.append(True),
        state_path=tmp_path / "trigger.json",
        environ={"SIWAH_EVOLVE": "off"},
    )
    assert not trigger.after_session(store, ids[0]).triggered

    trigger.environ = {}
    set_paused(True)
    assert not trigger.after_session(store, ids[0]).triggered
    set_paused(False)

    trigger.environ = {"SIWAH_EVOLVE_AUTO": "0"}
    assert not trigger.after_session(store, ids[0]).triggered
    assert spawned == []


def test_trigger_needs_enough_sessions_without_a_severe_signal(store: Store, tmp_path: Path):
    session = store.create_session(cwd=str(Path.cwd()), model="mock/model-a")
    spawned: list[bool] = []
    trigger = EvolutionTrigger(
        EvolutionConfig(auto=True, min_sessions=3),
        spawn=lambda: spawned.append(True),
        state_path=tmp_path / "trigger.json",
        environ={},
    )
    decision = trigger.after_session(store, session.id)
    assert not decision.triggered
    assert decision.reason == "not enough sessions"
    assert spawned == []
