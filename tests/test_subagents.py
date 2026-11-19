from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from siwah.config import AgentConfig, Config, PermissionsConfig
from siwah.core.storage import Session, Store
from siwah.core.subagents import SubagentManager, SubagentResult, load_subagents
from siwah.permissions.policy import PermissionPolicy
from siwah.tools.base import ToolContext
from siwah.tools.task import TaskTool

from .conftest import ScriptedAdapter, make_loop, outcome, tool_call


def _context(tmp_path: Path, manager: Any) -> ToolContext:
    return ToolContext(
        workspace=tmp_path,
        policy=PermissionPolicy(tmp_path, PermissionsConfig()),
        session_id="ses_parent",
        agent_config=AgentConfig(),
        subagent_manager=manager,
    )


def test_custom_subagent_loader_and_read_only_filter(tmp_path: Path):
    root = tmp_path / "subagents"
    root.mkdir()
    (root / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: Review changes\nread_only: true\n"
        "tools: read_file, write_file, task\n---\nReview carefully.\n",
        encoding="utf-8",
    )
    specs = load_subagents(root)
    spec = specs["reviewer"]
    assert spec.read_only
    assert spec.allowed_tools({"read_file", "write_file", "task"}) == {"read_file"}


def test_subagent_specs_reload_after_file_created(config: Config, store: Store, sample_repo: Path):
    loop = make_loop(config, store, ScriptedAdapter([], config.providers["mock"]))
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")

    async def emit(_event_type: str, _data: dict) -> None:
        return None

    root = sample_repo / "global-subagents"
    root.mkdir()
    manager = SubagentManager(
        config=config,
        store=store,
        router=loop.router,
        tools=[],
        parent_session=session,
        emit=emit,
        root=root,
    )
    (root / "bx-explorer.md").write_text(
        "---\nname: bx-explorer\ndescription: Explore code without edits\n"
        "read_only: true\n---\nReport with EXPLORER-OK.\n",
        encoding="utf-8",
    )
    manager.reload_specs()
    assert "bx-explorer" in manager.specs
    assert manager.specs["bx-explorer"].read_only


@pytest.mark.asyncio
async def test_task_tool_returns_isolated_child_result(tmp_path: Path):
    class FakeManager:
        async def run(self, name: str, prompt: str, *, description: str = ""):
            return SubagentResult(
                name=name,
                text=f"found: {prompt}",
                session_id="ses_child",
                usage=None,
            )

    tool = TaskTool()
    result = await tool.run(
        _context(tmp_path, FakeManager()),
        subagent_type="explore",
        prompt="inspect the repo",
    )
    assert result.ok
    assert "found: inspect the repo" in result.output
    assert result.data is not None and result.data["session_id"] == "ses_child"


@pytest.mark.asyncio
async def test_agent_loop_runs_a_child_session(config, store: Store, sample_repo: Path):
    config = config.model_copy(
        update={"subagents": config.subagents.model_copy(update={"max_steps": 3})}
    )
    adapter = ScriptedAdapter(
        [
            outcome(
                tool_calls=[
                    tool_call(
                        "task",
                        {
                            "subagent_type": "explore",
                            "prompt": "inspect README",
                            "description": "readme",
                        },
                    )
                ]
            ),
            outcome(text="child found the README"),
            outcome(text="parent done"),
        ],
        config.providers["mock"],
    )
    loop = make_loop(config, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")

    async def emit(_event: str, _data: dict) -> None:
        return None

    result = await loop.run(session, "delegate the inspection", emit=emit, max_steps=5)

    assert result.text == "parent done"
    children = [
        item
        for item in store.list_sessions(limit=10)
        if item.metadata.get("parent_session_id") == session.id
    ]
    assert len(children) == 1
    assert children[0].metadata["subagent"] == "explore"
    assert children[0].metadata["depth"] == 1


def test_subagent_manager_specs_include_defaults(tmp_path: Path):
    async def emit(_event: str, _data: dict) -> None:
        return None

    store = Store(tmp_path / "state.db")
    manager = SubagentManager(
        config=Config(),
        store=store,
        router=object(),  # type: ignore[arg-type]
        tools=[],
        parent_session=Session(id="ses", cwd=str(tmp_path), created_at="now", updated_at="now"),
        emit=emit,
        root=tmp_path / "empty",
    )
    assert {"explore", "general", "review"} <= set(manager.specs)
    store.close()
