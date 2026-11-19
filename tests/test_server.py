from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from siwah.adapt.models import EvolutionState
from siwah.adapt.promoter import HarnessRepository
from siwah.adapt.state import is_paused, save_state
from siwah.auth import AuthStore
from siwah.config import AuthConfig, Config, ProviderConfig
from siwah.core.loop import AgentLoop
from siwah.core.storage import Store
from siwah.harness.bootstrap import ensure_harness
from siwah.harness.skills import create_skill
from siwah.kernel.audit import audit_event
from siwah.paths import harness_dir
from siwah.providers.registry import ProviderRegistry, RoleRouter
from siwah.server.app import create_app
from siwah.server.runtime import DaemonRuntime
from siwah.tools.base import ToolRegistry
from siwah.tools.runtime import default_tools

from .conftest import ScriptedAdapter, outcome, tool_call


async def make_runtime(config, store: Store, sample_repo: Path) -> DaemonRuntime:
    adapter = ScriptedAdapter(
        [
            outcome(tool_calls=[tool_call("read_file", {"path": "README.md"})]),
            outcome(text="The README is short.", allowance=0.001),
        ],
        config.providers["mock"],
    )
    registry = ProviderRegistry(config, AuthStore())
    registry._adapters["mock"] = adapter  # type: ignore[assignment]
    router = RoleRouter(config, registry)
    loop = AgentLoop(
        config=config,
        store=store,
        router=router,
        tools=ToolRegistry(default_tools()),
    )
    return DaemonRuntime(
        config,
        store=store,
        registry=registry,
        router=router,
        loop=loop,
    )


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_stale_running_session_is_reported_idle(config, store: Store, sample_repo: Path):
    runtime = await make_runtime(config, store, sample_repo)
    app = create_app(config, runtime=runtime, token="test-token")
    transport = ASGITransport(app=app)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    store.update_session(session.id, status="running")
    headers = {"Authorization": "Bearer test-token"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        one = await client.get(f"/v1/sessions/{session.id}", headers=headers)
        assert one.status_code == 200
        assert one.json()["status"] == "idle"
        listed = await client.get("/v1/sessions", headers=headers)
        assert listed.status_code == 200
        listed_session = next(
            item for item in listed.json()["sessions"] if item["id"] == session.id
        )
        assert listed_session["status"] == "idle"
        aborted = await client.post(f"/v1/sessions/{session.id}/abort", headers=headers)
        assert aborted.status_code == 200
        assert aborted.json()["ok"] is False


@pytest.mark.asyncio
async def test_prompt_is_queued_while_a_run_is_active(config, store: Store, sample_repo: Path):
    runtime = await make_runtime(config, store, sample_repo)
    app = create_app(config, runtime=runtime, token="test-token")
    transport = ASGITransport(app=app)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    blocker = asyncio.create_task(asyncio.sleep(30))
    runtime._tasks[session.id] = blocker
    headers = {"Authorization": "Bearer test-token"}
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            queued = await client.post(
                f"/v1/sessions/{session.id}/messages",
                headers=headers,
                json={"content": "queued prompt"},
            )
            assert queued.status_code == 200, queued.text
            assert queued.json()["status"] == "queued"
            assert len(runtime._queues[session.id]) == 1
    finally:
        blocker.cancel()
        await asyncio.gather(blocker, return_exceptions=True)
        runtime._tasks.pop(session.id, None)
    runtime._drain_queue(session.id)
    for _ in range(100):
        await asyncio.sleep(0.02)
        if not runtime.is_running(session.id):
            break
    messages = store.get_messages(session.id)
    assert [message.content for message in messages if message.role == "user"] == ["queued prompt"]
    assert any(message.role == "assistant" and message.content for message in messages)
    stored = store.get_session(session.id)
    assert stored is not None
    assert stored.status == "idle"


async def test_server_auth_sessions_and_sse(config, store: Store, sample_repo: Path):
    runtime = await make_runtime(config, store, sample_repo)
    app = create_app(config, runtime=runtime, token="test-token")
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unauthorized = await client.get("/v1/meta")
        assert unauthorized.status_code == 401
        headers = {"Authorization": "Bearer test-token"}
        meta = await client.get("/v1/meta", headers=headers)
        assert meta.status_code == 200
        assert meta.json()["protocol"] == "0.1"

        created = await client.post(
            "/v1/sessions",
            headers=headers,
            json={"cwd": str(sample_repo), "model": "mock/model-a"},
        )
        assert created.status_code == 200, created.text
        session_id = created.json()["id"]

        sent = await client.post(
            f"/v1/sessions/{session_id}/messages",
            headers=headers,
            json={"content": "Read the README"},
        )
        assert sent.status_code == 200, sent.text
        assert sent.json()["status"] == "running"

        await asyncio.sleep(0.2)
        history = runtime.bus(session_id).history(0)
        types = [event.type for event in history]
        assert "message.delta" in types
        assert "tool.end" in types
        assert "usage" in types
        assert types[-1] == "run.end"
        stored = store.get_session(session_id)
        assert stored is not None
        assert stored.status == "idle"

        message_response = await client.get(f"/v1/sessions/{session_id}/messages", headers=headers)
        assert message_response.status_code == 200
        assert [message["role"] for message in message_response.json()["messages"]] == [
            "user",
            "assistant",
            "tool",
            "assistant",
        ]
        usage = await client.get(f"/v1/sessions/{session_id}/usage", headers=headers)
        assert usage.status_code == 200
        assert usage.json()["steps"] == 2

        files = await client.get(f"/v1/files?session_id={session_id}", headers=headers)
        assert files.status_code == 200
        assert "README.md" in files.json()["files"]
        cwd_files = await client.get(f"/v1/files?cwd={sample_repo}", headers=headers)
        assert cwd_files.status_code == 200
        assert "README.md" in cwd_files.json()["files"]

        patched = await client.patch(
            f"/v1/sessions/{session_id}",
            headers=headers,
            json={"model": "mock/model-b", "mode": "plan"},
        )
        assert patched.status_code == 200
        assert patched.json()["mode"] == "plan"
        assert patched.json()["model"] == "mock/model-b"

        forked = await client.post(f"/v1/sessions/{session_id}/fork", headers=headers, json={})
        assert forked.status_code == 200
        assert forked.json()["session"]["id"] != session_id

        models = await client.get("/v1/models?provider=mock", headers=headers)
        assert models.status_code == 200
        assert models.json()["providers"][0]["models"][0]["id"] == "model-a"

        all_models = await client.get("/v1/models", headers=headers)
        assert all_models.status_code == 200
        assert all_models.json()["providers"][0]["models"][0]["id"] == "model-a"

        create_skill("probe-skill", "A test skill", "Do the probe.")
        skills = await client.get("/v1/harness/skills", headers=headers)
        assert skills.status_code == 200
        assert skills.json()["skills"][0]["name"] == "probe-skill"
        skill = await client.get("/v1/harness/skills/probe-skill", headers=headers)
        assert skill.status_code == 200
        assert "Do the probe." in skill.json()["content"]

        memory = await client.get(f"/v1/harness/memory?cwd={sample_repo}", headers=headers)
        assert memory.status_code == 200
        assert memory.json()["user"]["path"].endswith("user.md")
        init_memory = await client.post(
            "/v1/harness/init", headers=headers, json={"cwd": str(sample_repo)}
        )
        assert init_memory.status_code == 200
        assert init_memory.json()["created"] is True
        assert init_memory.json()["path"].endswith("AGENTS.md")
        project_memory = sample_repo / ".siwah" / "project.md"
        assert project_memory.is_file()
        assert "Project memory" in project_memory.read_text(encoding="utf-8")
        note = await client.post(
            "/v1/harness/note", headers=headers, json={"content": "remember this"}
        )
        assert note.status_code == 200
        assert note.json()["changed"] is True

        blocked_shell = await client.post(
            f"/v1/sessions/{session_id}/shell",
            headers=headers,
            json={"command": "echo shell-ok"},
        )
        assert blocked_shell.status_code == 409
        allowed_shell = await client.post(
            f"/v1/sessions/{session_id}/shell",
            headers=headers,
            json={"command": "echo shell-ok", "approved": True},
        )
        assert allowed_shell.status_code == 200
        assert "shell-ok" in allowed_shell.json()["output"]

        default_providers = await client.post("/v1/providers/defaults", headers=headers)
        assert default_providers.status_code == 200
        assert default_providers.json()["providers"][0]["provider"] == "mock"

        auth_status = await client.get("/v1/auth/status", headers=headers)
        assert auth_status.status_code == 200
        assert auth_status.json()["providers"][0]["provider"] == "mock"
        assert auth_status.json()["providers"][0]["scheme"] == "none"
        logged_in = await client.post(
            "/v1/auth/login",
            headers=headers,
            json={"provider": "mock", "api_key": "test-key"},
        )
        assert logged_in.status_code == 200
        auth_after = await client.get("/v1/auth/status", headers=headers)
        assert auth_after.json()["providers"][0]["file_set"] is True

        missing = await client.post(
            f"/v1/sessions/{session_id}/permissions/perm_missing",
            headers=headers,
            json={"decision": "once"},
        )
        assert missing.status_code == 404


@pytest.mark.asyncio
async def test_openapi_and_error_shape(config, store: Store, sample_repo: Path):
    app = create_app(config, runtime=None, token=None)
    schema = app.openapi()
    assert "/v1/sessions/{session_id}/events" in schema["paths"]
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/sessions",
            json={"cwd": str(sample_repo)},
        )
        assert response.status_code == 503
        body = response.json()
        assert body["error"]["code"] == "runtime_unavailable"


def test_list_files_falls_back_for_untracked_directories(tmp_path: Path):
    from siwah.server.runtime import _list_files

    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    files = _list_files(tmp_path, 10)
    assert "a.py" in files
    assert "README.md" in files


@pytest.mark.asyncio
async def test_provider_defaults_endpoint_creates_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SIWAH_CONFIG", raising=False)
    config = Config()
    runtime = DaemonRuntime(config, store=Store(tmp_path / "state.db"))
    app = create_app(config, runtime=runtime, token="test-token")
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/providers/defaults",
                headers={"Authorization": "Bearer test-token"},
            )
        assert response.status_code == 200
        names = {provider["provider"] for provider in response.json()["providers"]}
        assert names == {"opencode-go", "openai"}
        assert (tmp_path / "home" / "harness" / "config.toml").exists()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_models_skip_providers_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("SIWAH_TEST_MISSING_KEY", raising=False)
    config = Config(
        providers={
            "offline": ProviderConfig(
                api="openai_chat",
                base_url="http://127.0.0.1:1/v1",
                models_url="http://127.0.0.1:1/v1/models",
                auth=AuthConfig(scheme="bearer", env="SIWAH_TEST_MISSING_KEY"),
            )
        }
    )
    runtime = DaemonRuntime(
        config,
        store=Store(tmp_path / "state.db"),
        auth=AuthStore(tmp_path / "auth.json"),
    )
    app = create_app(config, runtime=runtime, token="test-token")
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/v1/models", headers={"Authorization": "Bearer test-token"}
            )
        assert response.status_code == 200
        assert "credential missing" in response.json()["providers"][0]["error"]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_evolution_endpoint_reads_state_and_audit(
    config, store: Store, sample_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    save_state(EvolutionState(tag="evo-3", status="canary"))
    audit_event("evolve.promoted", {"candidate": "seed-test", "tag": "evo-3"})

    runtime = await make_runtime(config, store, sample_repo)
    app = create_app(config, runtime=runtime, token="test-token")
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/v1/evolution", headers={"Authorization": "Bearer test-token"}
            )
        assert response.status_code == 200
        payload = response.json()
        assert payload["tag"] == "evo-3"
        assert payload["status"] == "canary"
        assert payload["events"][0]["event"] == "evolve.promoted"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_evolution_pause_and_resume(
    config, store: Store, sample_repo: Path, tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    runtime = await make_runtime(config, store, sample_repo)
    app = create_app(config, runtime=runtime, token="test-token")
    headers = {"Authorization": "Bearer test-token"}
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            paused = await client.post(
                "/v1/evolution/pause", headers=headers, json={"paused": True}
            )
            assert paused.status_code == 200
            assert is_paused() is True
            resumed = await client.post(
                "/v1/evolution/pause", headers=headers, json={"paused": False}
            )
            assert resumed.status_code == 200
            assert is_paused() is False
    finally:
        await runtime.close()


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
@pytest.mark.asyncio
async def test_evolution_rollback_endpoint_restores_previous_revision(
    config, store: Store, sample_repo: Path, tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    ensure_harness()
    repo = HarnessRepository(harness_dir())
    repo.ensure()
    prompt = harness_dir() / "prompts" / "system.md"
    prompt.write_text("version one\n", encoding="utf-8")
    repo.commit("v1")
    repo.tag("evo-1")
    prompt.write_text("version two\n", encoding="utf-8")
    repo.commit("v2")
    repo.tag("evo-2")

    runtime = await make_runtime(config, store, sample_repo)
    app = create_app(config, runtime=runtime, token="test-token")
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/evolution/rollback",
                headers={"Authorization": "Bearer test-token"},
                json={},
            )
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True
        assert prompt.read_text(encoding="utf-8") == "version one\n"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_server_accepts_image_attachments(config, store: Store, sample_repo: Path):
    runtime = await make_runtime(config, store, sample_repo)
    app = create_app(config, runtime=runtime, token="test-token")
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": "Bearer test-token"}
        created = await client.post(
            "/v1/sessions",
            headers=headers,
            json={"cwd": str(sample_repo), "model": "mock/model-a"},
        )
        session_id = created.json()["id"]
        sent = await client.post(
            f"/v1/sessions/{session_id}/messages",
            headers=headers,
            json={
                "content": "describe this",
                "attachments": [{"mime": "image/png", "data": "YWJj", "name": "pasted.png"}],
            },
        )
        assert sent.status_code == 200, sent.text
        await asyncio.sleep(0.2)
        messages = await client.get(f"/v1/sessions/{session_id}/messages", headers=headers)
        user = next(item for item in messages.json()["messages"] if item["role"] == "user")
        assert user["attachments"] == [{"mime": "image/png", "data": "YWJj", "name": "pasted.png"}]
