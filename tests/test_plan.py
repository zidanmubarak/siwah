from __future__ import annotations

from pathlib import Path

import pytest

from siwah.core.storage import Store

from .conftest import ScriptedAdapter, make_loop, outcome, tool_call


@pytest.mark.asyncio
async def test_plan_mode_filters_write_tools_and_writes_plan(
    config, store: Store, sample_repo: Path
):
    adapter = ScriptedAdapter(
        [outcome(text="1. Read math_utils.py\n2. Fix add.\n3. Run tests.")],
        config.providers["mock"],
    )
    loop = make_loop(config, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a", mode="plan")

    async def emit(_event_type: str, _data: dict) -> None:
        return None

    result = await loop.run(session, "Plan the fix", emit=emit)
    assert result.error is None
    names = {tool.name for tool in adapter.requests[0].tools}
    assert "write_file" not in names
    assert "edit_file" not in names
    assert "bash" not in names
    assert "repo_map" in names
    assert "skill_list" in names
    assert "skill_manage" not in names
    plan_path = sample_repo / ".siwah" / "plan.md"
    assert plan_path.exists()
    assert "Fix add." in plan_path.read_text(encoding="utf-8")
    updated = store.get_session(session.id)
    assert updated is not None
    assert updated.metadata["plan_path"].endswith("plan.md")


@pytest.mark.asyncio
async def test_plan_mode_rejects_a_hallucinated_write(config, store: Store, sample_repo: Path):
    adapter = ScriptedAdapter(
        [
            outcome(
                tool_calls=[
                    tool_call(
                        "write_file",
                        {"path": "math_utils.py", "content": "broken\n"},
                    )
                ]
            ),
            outcome(text="The file is unchanged; here is the plan."),
        ],
        config.providers["mock"],
    )
    loop = make_loop(config, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a", mode="plan")

    async def emit(_event_type: str, _data: dict) -> None:
        return None

    result = await loop.run(session, "Try to write", emit=emit)
    assert result.error is None
    assert (sample_repo / "math_utils.py").read_text() == ("def add(a, b):\n    return a - b\n")
    tool_messages = [
        message for message in store.get_messages(session.id) if message.role == "tool"
    ]
    assert tool_messages
    assert "unavailable in plan mode" in (tool_messages[0].content or "")
