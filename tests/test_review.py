from __future__ import annotations

from pathlib import Path

from siwah.core.review import review_prompt
from siwah.core.storage import Store
from siwah.core.types import Message


def test_review_prompt_builds_a_bounded_digest(store: Store, sample_repo: Path):
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a", title="review me")
    store.append_message(
        session.id,
        Message(role="user", content="Fix the parser and run the tests."),
    )
    store.append_message(
        session.id,
        Message(role="assistant", content="Changed parser.py; tests pass."),
    )
    prompt = review_prompt(store, session, steps=12)
    assert "review me" in prompt
    assert "Fix the parser" in prompt
    assert "Steps: 12" in prompt
    assert "NO_LEARNING" in prompt
    assert len(prompt) < 20_000


def test_background_review_is_opt_in_by_default():
    from siwah.config import AgentConfig

    assert AgentConfig().background_review is False
    assert AgentConfig().background_review_min_steps >= 1
