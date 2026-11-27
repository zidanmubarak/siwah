from __future__ import annotations

import asyncio
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
        async def run(self, name: str, prompt: str, *, description: str = "", **_: Any):
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

    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(event: str, data: dict) -> None:
        events.append((event, data))

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
    parent_call = store.get_tool_calls(session.id)[0]["id"]
    assert children[0].metadata["parent_tool_call_id"] == parent_call
    child_events = [(name, data) for name, data in events if name.startswith("subagent.")]
    assert child_events[0][0] == "subagent.start"
    assert all(
        data["task_call_id"] == parent_call and data["session_id"] == children[0].id
        for _name, data in child_events
    )
    assert any(
        name == "subagent.event"
        and data["event"] == "message.start"
        and data["data"]["role"] == "assistant"
        for name, data in child_events
    )
    assert child_events[-1][0] == "subagent.end"
    assert child_events[-1][1]["status"] == "completed"
    parent_results = [
        message.content for message in adapter.requests[-1].messages if message.role == "tool"
    ]
    assert parent_results == ["child found the README"]


async def test_aborting_parent_reports_cancelled_child(config, store: Store, sample_repo: Path):
    class BlockingAdapter(ScriptedAdapter):
        async def stream_chat(self, request, *, session_id, emit):
            if self.requests:
                child_started.set()
                await asyncio.Future()
            return await super().stream_chat(request, session_id=session_id, emit=emit)

    child_started = asyncio.Event()
    adapter = BlockingAdapter(
        [outcome(tool_calls=[tool_call("task", {"subagent_type": "explore", "prompt": "wait"})])],
        config.providers["mock"],
    )
    loop = make_loop(config, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(event: str, data: dict) -> None:
        events.append((event, data))

    task = asyncio.create_task(loop.run(session, "delegate", emit=emit, max_steps=5))
    try:
        await asyncio.wait_for(child_started.wait(), timeout=3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    child_end = next(data for name, data in events if name == "subagent.end")
    assert child_end["status"] == "cancelled"
    child = store.get_session(child_end["session_id"])
    assert child is not None and child.status == "idle"
    assert len(adapter.requests) == 1


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
