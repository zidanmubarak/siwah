from __future__ import annotations

from pathlib import Path

from siwah.adapt.observer import Observer
from siwah.core.storage import Store
from siwah.core.types import Message


def test_observer_summarizes_and_redacts_sessions(tmp_path: Path):
    store = Store(tmp_path / "state.db")
    try:
        session = store.create_session(cwd=str(tmp_path), model="mock/model-a")
        store.append_message(session.id, Message(role="user", content="fix the bug"))
        store.append_message(session.id, Message(role="assistant", content="working"))
        store.append_message(
            session.id,
            Message(role="user", content="use sk-abcdefghijklmnopqrstuvwx"),
        )
        store.update_session(session.id, status="error")

        observations = Observer().observe(store, limit=5)

        assert len(observations) == 1
        observation = observations[0]
        assert observation.user_messages == 2
        assert observation.assistant_messages == 1
        assert "sk-abcdefghijklmnopqrstuvwx" not in observation.text
        signals = Observer().signals(observations)
        assert any("unfinished session" in signal for signal in signals)
    finally:
        store.close()
