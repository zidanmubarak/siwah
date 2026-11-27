from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from siwah.auth import AuthStore
from siwah.config import Config, JobsConfig, PermissionsConfig
from siwah.core.background import BackgroundResult
from siwah.core.loop import AgentLoop
from siwah.core.storage import Store
from siwah.core.subagents import SubagentManager
from siwah.core.types import Message, ProviderEvent
from siwah.jobs import JobManager
from siwah.providers.registry import ProviderRegistry, RoleRouter
from siwah.server.runtime import DaemonRuntime
from siwah.tools.base import ToolRegistry
from siwah.tools.runtime import default_tools

from .conftest import ScriptedAdapter, outcome, tool_call


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_background_subagent_returns_then_delivers_its_result(
    config: Config, store: Store, sample_repo: Path
):
    adapter = ScriptedAdapter([outcome(text="child found the README")], config.providers["mock"])
    registry = ProviderRegistry(config, AuthStore())
    registry._adapters["mock"] = adapter
    router = RoleRouter(config, registry)
    loop = AgentLoop(
        config=config,
        store=store,
        router=router,
        tools=ToolRegistry(default_tools()),
    )
    parent = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    events: list[tuple[str, dict[str, Any]]] = []
    delivered: asyncio.Queue[BackgroundResult] = asyncio.Queue()

    async def emit(event: str, data: dict[str, Any]) -> None:
        events.append((event, data))

    async def sink(result: BackgroundResult) -> None:
        await delivered.put(result)

    manager = SubagentManager(
        config=config,
        store=store,
        router=router,
        tools=loop.tools.tools(),
        parent_session=parent,
        emit=emit,
        background_sink=sink,
    )
    started = await manager.run("explore", "inspect the README", background=True)
    assert started.background is True
    assert "Started explore in the background" in started.text

    result = await asyncio.wait_for(delivered.get(), timeout=5)
    assert result.kind == "subagent"
    assert result.session_id == parent.id
    assert result.actor_id == started.session_id
    assert result.status == "completed"
    assert "child found the README" in result.text
    assert any(name == "subagent.end" for name, _ in events)


@pytest.mark.asyncio
async def test_job_completion_callback_receives_output(tmp_path: Path):
    completed: asyncio.Queue[Any] = asyncio.Queue()

    async def on_complete(job):
        await completed.put(job)

    manager = JobManager(JobsConfig(), cwd=tmp_path, on_complete=on_complete)
    try:
        await manager.start("python -c \"print('job done')\"")
        job = await asyncio.wait_for(completed.get(), timeout=5)
        assert job.status == "completed"
        assert job.returncode == 0
        assert "job done" in "".join(job.output)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_background_shell_job_notifies_the_parent(
    config: Config, store: Store, sample_repo: Path
):
    cfg = config.model_copy(update={"permissions": PermissionsConfig(mode="yolo")})
    adapter = ScriptedAdapter(
        [
            outcome(
                tool_calls=[
                    tool_call(
                        "bash",
                        {"command": "python -c \"print('job done')\"", "background": True},
                    )
                ]
            ),
            outcome(text="parent finished"),
        ],
        cfg.providers["mock"],
    )
    registry = ProviderRegistry(cfg, AuthStore())
    registry._adapters["mock"] = adapter
    router = RoleRouter(cfg, registry)
    loop = AgentLoop(
        config=cfg,
        store=store,
        router=router,
        tools=ToolRegistry(default_tools()),
    )
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    delivered: asyncio.Queue[BackgroundResult] = asyncio.Queue()

    async def sink(result: BackgroundResult) -> None:
        await delivered.put(result)

    async def emit(_event: str, _data: dict[str, Any]) -> None:
        pass

    try:
        await loop.run(session, "start a background job", emit=emit, background_sink=sink)
        result = await asyncio.wait_for(delivered.get(), timeout=5)
        assert result.kind == "job"
        assert result.status == "completed"
        assert "job done" in result.text
        assert result.tool_call_id and result.tool_call_id.startswith("call_")
    finally:
        await loop.close_jobs()


@pytest.mark.asyncio
async def test_background_result_starts_a_continuation_run(
    config: Config, store: Store, sample_repo: Path
):
    adapter = ScriptedAdapter(
        [outcome(text="I handled the background result.", allowance=0.001)],
        config.providers["mock"],
    )
    registry = ProviderRegistry(config, AuthStore())
    registry._adapters["mock"] = adapter
    router = RoleRouter(config, registry)
    loop = AgentLoop(
        config=config,
        store=store,
        router=router,
        tools=ToolRegistry(default_tools()),
    )
    runtime = DaemonRuntime(config, store=store, registry=registry, router=router, loop=loop)
    session = runtime.create_session(cwd=str(sample_repo), model="mock/model-a")
    try:
        await runtime._deliver_background_result(
            BackgroundResult(
                kind="job",
                session_id=session.id,
                actor_id="job_1",
                title="python -c print('ok')",
                status="completed",
                text="ok",
                returncode=0,
            )
        )
        await _wait_until(lambda: not runtime.is_running(session.id))
        messages = store.get_messages(session.id)
        assert any(
            message.role == "system" and "Background result" in (message.content or "")
            for message in messages
        )
        assert any(
            message.role == "assistant" and "handled" in (message.content or "")
            for message in messages
        )
        assert adapter.requests and any(
            message.role == "system" and "Background result" in (message.content or "")
            for message in adapter.requests[0].messages
        )
        assert any(event.type == "background.result" for event in runtime.bus(session.id).history())
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_background_task_tool_runs_while_the_parent_continues(
    config: Config, store: Store, sample_repo: Path
):
    class FlowAdapter(ScriptedAdapter):
        def __init__(self, provider_config):
            super().__init__([], provider_config)
            self.parent_calls = 0
            self.child_done = asyncio.Event()
            self.parent_id = ""

        async def stream_chat(self, request, *, session_id, emit):
            self.requests.append(request)
            if session_id == self.parent_id:
                self.parent_calls += 1
                if self.parent_calls == 1:
                    result = outcome(
                        tool_calls=[
                            tool_call(
                                "task",
                                {
                                    "subagent_type": "explore",
                                    "prompt": "inspect the README",
                                    "background": True,
                                },
                            )
                        ]
                    )
                elif self.parent_calls == 2:
                    await asyncio.wait_for(self.child_done.wait(), timeout=5)
                    result = outcome(text="parent finished")
                else:
                    result = outcome(text="handled the background result")
            else:
                self.child_done.set()
                result = outcome(text="child found the README")
            await emit(ProviderEvent(type="usage", usage=result.usage))
            await emit(ProviderEvent(type="finish", finish_reason=result.finish_reason))
            return result

    adapter = FlowAdapter(config.providers["mock"])
    registry = ProviderRegistry(config, AuthStore())
    registry._adapters["mock"] = adapter
    router = RoleRouter(config, registry)
    loop = AgentLoop(
        config=config,
        store=store,
        router=router,
        tools=ToolRegistry(default_tools()),
    )
    runtime = DaemonRuntime(config, store=store, registry=registry, router=router, loop=loop)
    session = runtime.create_session(cwd=str(sample_repo), model="mock/model-a")
    adapter.parent_id = session.id
    try:
        runtime.start_run(session, "delegate the README inspection")
        await _wait_until(
            lambda: any(
                message.role == "assistant"
                and "handled the background result" in (message.content or "")
                for message in store.get_messages(session.id)
            )
        )
        messages = store.get_messages(session.id)
        assert any(
            message.role == "system" and "child found the README" in (message.content or "")
            for message in messages
        )
        assert any(
            message.role == "assistant" and "parent finished" in (message.content or "")
            for message in messages
        )
        assert any(
            message.role == "tool"
            and "Started explore in the background" in (message.content or "")
            for message in messages
        )
        assert len(adapter.requests) >= 4
        assert any(
            message.role == "system" and "Background result" in (message.content or "")
            for message in adapter.requests[-1].messages
        )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_continuation_skips_a_synthetic_user_message(
    config: Config, store: Store, sample_repo: Path
):
    adapter = ScriptedAdapter([outcome(text="continued")], config.providers["mock"])
    registry = ProviderRegistry(config, AuthStore())
    registry._adapters["mock"] = adapter
    router = RoleRouter(config, registry)
    loop = AgentLoop(
        config=config,
        store=store,
        router=router,
        tools=ToolRegistry(default_tools()),
    )
    runtime = DaemonRuntime(config, store=store, registry=registry, router=router, loop=loop)
    session = runtime.create_session(cwd=str(sample_repo), model="mock/model-a")
    store.append_message(session.id, Message(role="system", content="notice"))
    try:
        runtime._start_run(session, "", continuation=True)
        await _wait_until(lambda: not runtime.is_running(session.id))
        assert [message.role for message in store.get_messages(session.id)] == [
            "system",
            "assistant",
        ]
    finally:
        await runtime.close()
