from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from siwah.core.storage import Store
from siwah.core.types import Message, StreamOutcome, Usage
from siwah.harness.lessons import write_lesson
from siwah.harness.skill_usage import record_event
from siwah.harness.skills import create_skill
from siwah.permissions.policy import PermissionRequest
from siwah.sandbox import Sandbox, SandboxResult
from siwah.server.app import create_app
from siwah.server.events import EventBus

from .test_server import make_runtime


async def test_learning_endpoint_reports_skills_and_lessons(config, store, sample_repo):
    runtime = await make_runtime(config, store, sample_repo)
    create_skill("verify", "Verify a change.", "Run tests.")
    record_event("verify", "create", created_by="agent")
    write_lesson(
        sample_repo,
        target="project",
        cue="Use the focused test",
        lesson="Run the focused test before the full suite.",
        evidence="pytest -q tests/test_math_utils.py",
    )
    app = create_app(config, runtime=runtime, token="test-token")
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                f"/v1/harness/learning?cwd={sample_repo.as_posix()}",
                headers={"Authorization": "Bearer test-token"},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["skills"][0]["name"] == "verify"
        assert body["skills"][0]["state"] == "active"
        assert body["lessons"][0]["cue"] == "Use the focused test"
    finally:
        await runtime.close()


async def test_tool_activity_does_not_evict_running_work_first():
    bus = EventBus("session", retention=10)
    for index in range(128):
        await bus.publish(
            "job.completed",
            {"tool_call_id": f"call-{index}", "id": f"job-{index}", "status": "completed"},
        )
    await bus.publish(
        "job.started",
        {"tool_call_id": "active", "id": "active-job", "status": "running"},
    )
    await bus.publish(
        "job.completed",
        {"tool_call_id": "late", "id": "late-job", "status": "completed"},
    )
    assert any(item["data"].get("id") == "active-job" for item in bus.tool_activity())


async def test_session_tool_progress_recovers_after_event_replay_expires(
    config, store, sample_repo
):
    runtime = await make_runtime(config, store, sample_repo)
    session = runtime.create_session(cwd=str(sample_repo), model="mock/model-a")
    bus = runtime.bus(session.id)
    child = {"task_call_id": "task", "session_id": "child", "subagent": "explore"}
    await bus.publish("subagent.start", {**child, "description": "internal task prompt"})
    await bus.publish(
        "subagent.event",
        {**child, "event": "tool.start", "data": {"name": "read_file", "arguments": "raw"}},
    )
    await bus.publish(
        "job.progress",
        {
            "tool_call_id": "shell",
            "id": "job",
            "status": "running",
            "lines_read": 5,
            "output": "raw",
        },
    )
    for _ in range(config.server.event_retention):
        await bus.publish("message.delta", {"text": "later"})
    assert bus.replay_gap(1)

    app = create_app(config, runtime=runtime, token="test-token")
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                f"/v1/sessions/{session.id}", headers={"Authorization": "Bearer test-token"}
            )
        assert response.status_code == 200
        assert response.json()["metadata"]["tool_activity"] == [
            {
                "seq": 2,
                "type": "subagent.event",
                "data": {**child, "event": "tool.start", "data": {"name": "read_file"}},
            },
            {
                "seq": 3,
                "type": "job.progress",
                "data": {
                    "tool_call_id": "shell",
                    "id": "job",
                    "status": "running",
                    "lines_read": 5,
                },
            },
        ]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_protocol_mismatch_rejected_before_session_creation(config, store, sample_repo):
    runtime = await make_runtime(config, store, sample_repo)
    app = create_app(config, runtime=runtime, token="test-token")
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {"Authorization": "Bearer test-token", "X-Siwah-Protocol": "99.0"}
            response = await client.post(
                "/v1/sessions", headers=headers, json={"cwd": str(sample_repo)}
            )
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "protocol_mismatch"
            assert store.list_sessions() == []
            headers["X-Siwah-Protocol"] = "0.1"
            assert (await client.get("/v1/meta", headers=headers)).status_code == 200
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_workspace_mutations_rejected_while_another_session_runs(
    config, store: Store, sample_repo: Path, monkeypatch
):
    runtime = await make_runtime(config, store, sample_repo)
    source = runtime.create_session(cwd=str(sample_repo), model="mock/model-a")
    other = runtime.create_session(cwd=str(sample_repo), model="mock/model-a")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def waiting_run(*_args, **_kwargs):
        entered.set()
        await release.wait()

    monkeypatch.setattr(runtime.loop, "run", waiting_run)
    runtime.start_run(other, "Keep working")
    await entered.wait()
    app = create_app(config, runtime=runtime, token="test-token")
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {"Authorization": "Bearer test-token"}
            base = f"/v1/sessions/{source.id}"
            before_sessions = store.session_ids()
            for endpoint, body in [
                ("/checkpoints", {"label": "must not snapshot a changing workspace"}),
                ("/rewind", {"checkpoint_id": "missing", "confirm": True}),
                ("/fork", {"checkpoint_id": "missing"}),
                ("/shell", {"command": "echo forbidden > marker.txt", "approved": True}),
                ("/compact", {}),
            ]:
                response = await client.post(base + endpoint, headers=headers, json=body)
                assert response.status_code == 409, (endpoint, response.text)
                assert response.json()["error"]["code"] == "session_busy"
            assert not (sample_repo / "marker.txt").exists()
            assert store.list_checkpoints(source.id) == []
            assert store.session_ids() == before_sessions
            assert store.get_session(source.id).metadata == {}  # type: ignore[union-attr]
    finally:
        release.set()
        await runtime.close()


@pytest.mark.asyncio
async def test_manual_compaction_reserves_session_and_preserves_prior_summary(
    config, store: Store, sample_repo: Path, monkeypatch
):
    config.roles.fast = "mock/model-a"
    runtime = await make_runtime(config, store, sample_repo)
    session = runtime.create_session(cwd=str(sample_repo), model="mock/model-a")
    store.append_message(session.id, Message(role="user", content="Keep the original goal"))
    store.set_context_summary(session.id, "Prior decision: retain the compatibility API", 1)
    store.append_message(session.id, Message(role="user", content="Now add a regression test"))
    entered = asyncio.Event()
    release = asyncio.Event()
    prompts = []

    async def slow_summary(_role, *, messages, **_kwargs):
        prompts.extend(messages)
        entered.set()
        await release.wait()
        return StreamOutcome(provider="mock", model="model-a", text="A compact summary")

    monkeypatch.setattr(runtime.router, "stream", slow_summary)
    compaction = asyncio.create_task(runtime.compact_session(session.id))
    try:
        await entered.wait()
        with pytest.raises(RuntimeError, match="busy|active|operation"):
            runtime.start_run(session, "This message must not disappear")
        release.set()
        await compaction
        assert "Prior decision: retain the compatibility API" in prompts[-1].content
        assert len(store.get_messages(session.id)) == 2
    finally:
        release.set()
        await compaction
        await runtime.close()


@pytest.mark.asyncio
async def test_direct_shell_uses_configured_sandbox(config, store, sample_repo, monkeypatch):
    config.sandbox.enabled = True
    runtime = await make_runtime(config, store, sample_repo)
    session = runtime.create_session(cwd=str(sample_repo), model="mock/model-a")
    calls = []

    async def fake_shell(self, command, *, timeout_s=120.0, env_extra=None):
        calls.append((self.cwd, command, timeout_s))
        return SandboxResult(True, "isolated shell", 0, "mock")

    monkeypatch.setattr(Sandbox, "shell", fake_shell)
    try:
        result = await runtime.run_shell(session.id, "echo sandbox-check", approved=True)
        assert result["output"] == "isolated shell"
        assert calls == [(sample_repo.resolve(), "echo sandbox-check", 120.0)]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_context_meter_does_not_add_cached_tokens_twice(
    config, store, sample_repo, monkeypatch
):
    runtime = await make_runtime(config, store, sample_repo)
    session = runtime.create_session(cwd=str(sample_repo), model="mock/model-a")
    monkeypatch.setattr(runtime, "_context_window", lambda _model: 2000)
    store.record_usage(
        session_id=session.id,
        step_id="step-1",
        provider="mock",
        model=session.model,
        usage=Usage(input_tokens=1000, cache_read_tokens=800),
    )
    try:
        usage = runtime.usage(session)
        assert usage["context_tokens"] == 1000
        assert usage["context_percent"] == 50
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_permission_decision_must_match_request_session(config, store, sample_repo):
    runtime = await make_runtime(config, store, sample_repo)
    first = runtime.create_session(cwd=str(sample_repo), model="mock/model-a")
    second = runtime.create_session(cwd=str(sample_repo), model="mock/model-a")
    request = PermissionRequest(tool="bash", kind="bash", preview="$ test")
    waiter = asyncio.create_task(
        runtime._resolve_permission(request, "perm_test", session_id=first.id)
    )
    await asyncio.sleep(0)
    app = create_app(config, runtime=runtime, token="test-token")
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {"Authorization": "Bearer test-token"}
            wrong = await client.post(
                f"/v1/sessions/{second.id}/permissions/perm_test",
                headers=headers,
                json={"decision": "once"},
            )
            assert wrong.status_code == 404
            assert not waiter.done()
            correct = await client.post(
                f"/v1/sessions/{first.id}/permissions/perm_test",
                headers=headers,
                json={"decision": "deny"},
            )
            assert correct.status_code == 200
            assert (await waiter).decision == "deny"
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        await runtime.close()
