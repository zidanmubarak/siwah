from __future__ import annotations

import asyncio
import io
import json

import pytest

from siwah.acp import AcpServer
from siwah.core.loop import RunResult
from siwah.providers.registry import ProviderRegistry

from .conftest import ScriptedAdapter, outcome, tool_call


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,allowed", [("ask", False), ("auto-edit", True), ("yolo", True)])
async def test_acp_obeys_configured_tool_permission_mode(
    config, store, sample_repo, monkeypatch, mode, allowed
):
    config.permissions.mode = mode
    adapter = ScriptedAdapter(
        [
            outcome(
                tool_calls=[tool_call("write_file", {"path": "acp.txt", "content": "written"})]
            ),
            outcome(text="Finished the permitted work."),
        ],
        config.providers["mock"],
    )
    monkeypatch.setattr(ProviderRegistry, "adapter", lambda _self, _provider: adapter)
    server = AcpServer(config, store)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    await server._run(session, "Write acp.txt", emit)
    assert (sample_repo / "acp.txt").exists() is allowed
    if not allowed:
        assert any(
            kind == "permission.resolved" and data.get("decision") == "deny"
            for kind, data in events
        )


@pytest.mark.asyncio
async def test_acp_rejects_overlapping_prompts_without_losing_cancel_target(
    config, store, sample_repo
):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def runner(session, _text, _emit):
        entered.set()
        await release.wait()
        return RunResult(session_id=session.id)

    server = AcpServer(config, store, runner=runner)
    await server.handle({"method": "initialize", "id": 1})
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")

    def request(identifier):
        return {
            "method": "session/prompt",
            "id": identifier,
            "params": {"sessionId": session.id, "prompt": [{"type": "text", "text": "work"}]},
        }

    first = asyncio.create_task(server.handle(request(2)))
    try:
        await entered.wait()
        second = asyncio.create_task(server.handle(request(3)))
        await asyncio.sleep(0)
        # Release only after the second admission attempt; it must reject immediately.
        if not second.done():
            release.set()
        response = await second
        assert response is not None and "error" in response
        assert session.id in server._tasks
        await server.handle({"method": "session/cancel", "params": {"sessionId": session.id}})
        first_response = await first
        assert first_response is not None
        assert first_response["result"]["stopReason"] == "cancelled"
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_acp_finishes_admitted_prompt_before_returning_at_eof(config, store, sample_repo):
    async def runner(session, _text, _emit):
        await asyncio.sleep(0.01)
        return RunResult(session_id=session.id, text="finished")

    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    server = AcpServer(config, store, runner=runner)
    lines = [
        {"method": "initialize", "id": 1},
        {
            "method": "session/prompt",
            "id": 2,
            "params": {"sessionId": session.id, "prompt": [{"type": "text", "text": "work"}]},
        },
    ]
    output = io.StringIO()
    await server.serve(
        stdin=io.StringIO("\n".join(json.dumps(line) for line in lines)), stdout=output
    )
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert any(
        response["id"] == 2 and response["result"]["stopReason"] == "end_turn"
        for response in responses
    )
