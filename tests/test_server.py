from __future__ import annotations

import asyncio
import shutil
import sys
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
from siwah.permissions.policy import PermissionRequest
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
async def test_corrupt_chatgpt_store_is_reported_without_a_server_error(
    config, store: Store, tmp_path: Path
):
    chatgpt = tmp_path / "home" / "chatgpt" / "auth.json"
    chatgpt.parent.mkdir(parents=True)
    chatgpt.write_text("{broken", encoding="utf-8")
    cfg = config.model_copy(
        update={
            "providers": {
                **config.providers,
                "chatgpt": ProviderConfig(
                    api="openai_responses",
                    base_url="https://api.openai.com/v1",
                    models_url="https://api.openai.com/v1/models",
                    auth=AuthConfig(browser="chatgpt"),
                ),
            }
        }
    )
    runtime = DaemonRuntime(cfg, store=store)
    app = create_app(cfg, runtime=runtime, token="test-token")
    headers = {"Authorization": "Bearer test-token"}
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        status = await client.get("/v1/auth/status", headers=headers)
        assert status.status_code == 200, status.text
        row = next(item for item in status.json()["providers"] if item["provider"] == "chatgpt")
        assert row["file_set"] is False
        accounts = await client.get("/v1/auth/chatgpt/accounts", headers=headers)
        assert accounts.status_code == 400
        assert accounts.json()["error"]["code"] == "auth_store_error"


@pytest.mark.asyncio
async def test_abort_acknowledges_before_cleanup_and_publishes_terminal_event(
    config, store: Store, sample_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    runtime = await make_runtime(config, store, sample_repo)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    cleaning_up = asyncio.Event()
    release_cleanup = asyncio.Event()
    keep_running = asyncio.Event()

    async def start(*_args, **_kwargs):
        try:
            await keep_running.wait()
        finally:
            cleaning_up.set()
            await release_cleanup.wait()

    monkeypatch.setattr(runtime, "_run", start)
    run_id, _ = runtime.start_run(session, "work")
    task = runtime._tasks[session.id]
    app = create_app(config, runtime=runtime, token="test-token")
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            url = f"/v1/sessions/{session.id}/abort"
            headers = {"Authorization": "Bearer test-token"}
            for _ in range(2):
                response = await asyncio.wait_for(client.post(url, headers=headers), timeout=1)
                assert response.json() == {"ok": True}
            await asyncio.wait_for(cleaning_up.wait(), timeout=1)
            assert not task.done()
            assert task.cancelling() == 1
    finally:
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

    events = runtime.bus(session.id).history()
    ends = [event for event in events if event.type == "run.end"]
    assert [(event.turn_id, event.data) for event in ends] == [(run_id, {"reason": "aborted"})]
    assert not runtime.is_running(session.id)


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
            url = f"/v1/sessions/{session.id}"
            image = {"mime": "image/png", "data": "aW1hZ2U=", "name": "fixture.png"}
            second = await client.post(
                url + "/messages",
                headers=headers,
                json={"content": "edit later", "attachments": [image]},
            )
            first_id, second_id = queued.json()["run_id"], second.json()["run_id"]
            snapshot = (await client.get(url, headers=headers)).json()["prompt_queue"]
            assert [item["id"] for item in snapshot["items"]] == [first_id, second_id]
            assert snapshot["items"][1]["attachment_count"] == 1
            assert snapshot["event_seq"] == second.json()["event_id"]
            path = f"{url}/queue/{second_id}"
            assert (await client.delete(path)).status_code == 401
            removed = await client.delete(path, headers=headers)
            assert removed.json() == {
                "id": second_id,
                "content": "edit later",
                "attachments": [image],
            }
            assert (await client.delete(path, headers=headers)).status_code == 404
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
    events = runtime.bus(session.id).history()
    assert [event.data for event in events if event.type == "queue.changed"][-1] == {"items": []}
    assert any(event.data.get("queued_prompt_id") == first_id for event in events)


@pytest.mark.asyncio
async def test_unapplied_correction_can_be_resolved(config, store: Store, sample_repo: Path):
    runtime = DaemonRuntime(config, store=store)
    app = create_app(config, runtime=runtime, token="test-token")
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    store.record_unapplied_correction(session.id, "msg_dropped", "fix the typo")
    headers = {"Authorization": "Bearer test-token"}
    messages_url = f"/v1/sessions/{session.id}/messages"
    resolve_url = f"/v1/sessions/{session.id}/corrections/msg_dropped"
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        listed = await client.get(messages_url, headers=headers)
        assert listed.status_code == 200, listed.text
        assert any(
            message["id"] == "msg_dropped" and message.get("delivery") == "unapplied"
            for message in listed.json()["messages"]
        )
        assert (await client.delete(resolve_url)).status_code == 401
        resolved = await client.delete(resolve_url, headers=headers)
        assert resolved.status_code == 200, resolved.text
        assert resolved.json() == {"resolved": True}
        after = await client.get(messages_url, headers=headers)
        assert not any(message["id"] == "msg_dropped" for message in after.json()["messages"])
        assert (await client.delete(resolve_url, headers=headers)).status_code == 404


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
        assert {"mock", "openrouter", "chatgpt"} <= {
            item["provider"] for item in default_providers.json()["providers"]
        }

        auth_status = await client.get("/v1/auth/status", headers=headers)
        assert auth_status.status_code == 200
        assert (
            next(
                item["scheme"]
                for item in auth_status.json()["providers"]
                if item["provider"] == "mock"
            )
            == "none"
        )
        logged_in = await client.post(
            "/v1/auth/login",
            headers=headers,
            json={"provider": "mock", "api_key": "test-key"},
        )
        assert logged_in.status_code == 200
        auth_after = await client.get("/v1/auth/status", headers=headers)
        assert (
            next(
                item["file_set"]
                for item in auth_after.json()["providers"]
                if item["provider"] == "mock"
            )
            is True
        )

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
        assert names == {
            "opencode-go",
            "openai",
            "chatgpt",
            "openrouter",
            "deepseek",
            "commandcode",
        }
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


async def test_daemon_records_matching_canary_observations_across_reopen(
    config, store, sample_repo, monkeypatch
):
    monkeypatch.setattr(
        "siwah.server.runtime.load_state",
        lambda: EvolutionState(tag="evo-test", status="canary", canary_tag="evo-test"),
    )
    runtime = await make_runtime(config, store, sample_repo)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    before = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    store.record_run_observation(
        run_id="baseline-run",
        session=before,
        model="mock/model-a",
        tag="baseline",
        tools=1,
        errors=1,
        corrected=False,
        aborted=False,
        allowance=0.002,
    )
    runtime.submit_run(session, "Read this project")
    await runtime._tasks[session.id]
    reopen = Store(store.path)
    try:
        groups = reopen.compare_run_observations(
            before="baseline", after="evo-test", min_sessions=1
        )
        assert len(groups) == 1
        assert groups[0]["ready"] is True
        assert groups[0]["before"]["tool_error_rate"] == 1
        assert groups[0]["after"]["tool_error_rate"] == 0
        assert groups[0]["after"]["allowance_per_turn"] == pytest.approx(0.001)
    finally:
        reopen.close()


async def test_active_run_applies_correction_before_old_tools_and_keeps_timing(
    config, store, sample_repo, monkeypatch
):
    runtime = await make_runtime(config, store, sample_repo)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    started, release = asyncio.Event(), asyncio.Event()
    requests = []

    async def stream(role, *, messages, **kwargs):
        requests.append(list(messages))
        if len(requests) == 1:
            started.set()
            await release.wait()
            return outcome(
                tool_calls=[tool_call("write_file", {"path": "skip.txt", "content": "old"})]
            )
        return outcome(text="Correction followed.")

    monkeypatch.setattr(runtime.router, "stream", stream)
    app = create_app(config, runtime=runtime, token="test-token")
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        url = f"/v1/sessions/{session.id}"
        headers = {"Authorization": "Bearer test-token"}
        task = await client.post(url + "/messages", headers=headers, json={"content": "Start."})
        run_id = task.json()["run_id"]
        await asyncio.wait_for(started.wait(), 5)
        try:
            stale = await client.post(
                url + "/messages",
                headers=headers,
                json={"content": "Wrong target", "steer_run_id": "old"},
            )
            assert stale.status_code == 409
            accepted = await client.post(
                url + "/messages",
                headers=headers,
                json={"content": "Leave skip.txt alone.", "steer_run_id": run_id},
            )
            assert accepted.json()["status"] == "steering"
        finally:
            release.set()
            if runtime.is_running(session.id):
                await runtime._tasks[session.id]
        assert not (sample_repo / "skip.txt").exists()
        assert requests[1][-1].content == "Leave skip.txt alone."
        assert requests[1][-2].role == "tool"
        assert "superseded" in (requests[1][-2].content or "")
        notes = [
            item for item in runtime.bus(session.id).history() if item.type == "steering.applied"
        ]
        assert notes[0].data["message_id"] == accepted.json()["message_id"]
        assert any(
            item.content == "Leave skip.txt alone." for item in store.get_messages(session.id)
        )
        timing = (await client.get(url + "/timing", headers=headers)).json()
        assert timing["run_id"] == run_id
        durations = timing["durations"]
        assert durations["model"] > 0
        assert sum(value for name, value in durations.items() if name != "total") == pytest.approx(
            durations["total"], abs=0.05
        )


@pytest.mark.parametrize("parallel", [False, True])
async def test_steering_during_tools_skips_unstarted_calls(
    config, store, sample_repo, monkeypatch, parallel
):
    if not parallel:
        python = Path(sys.executable).as_posix()
        config.verify.auto = True
        config.permissions.mode = "yolo"
        (sample_repo / "check.py").write_text(
            "from pathlib import Path\nassert Path('file1.txt').read_text() == 'old'\n",
            encoding="utf-8",
        )
        (sample_repo / "verify.toml").write_text(
            f"[commands]\ntest = '\"{python}\" check.py'\n", encoding="utf-8"
        )
    runtime = await make_runtime(config, store, sample_repo)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    siblings = [f"file{index}.txt" for index in range(6 if parallel else 2)]
    for name in siblings:
        (sample_repo / name).write_text("old", encoding="utf-8")
    tool = "read_file" if parallel else "write_file"
    calls = [
        tool_call(tool, {"path": path, **({} if parallel else {"content": "new"})}, f"file-{index}")
        for index, path in enumerate(siblings)
    ]
    started, release = asyncio.Event(), asyncio.Event()
    executed, requests = [], []
    execute = runtime.loop.tools.execute

    async def blocking_tool(name, context, arguments):
        executed.append(arguments["path"])
        if arguments["path"] == siblings[0]:
            started.set()
            await release.wait()
        return await execute(name, context, arguments)

    async def stream(role, *, messages, **kwargs):
        requests.append(list(messages))
        return outcome(tool_calls=calls) if len(requests) == 1 else outcome(text="Updated.")

    monkeypatch.setattr(runtime.loop.tools, "execute", blocking_tool)
    monkeypatch.setattr(runtime.router, "stream", stream)
    run, _ = runtime.start_run(session, "Start")
    task = runtime._tasks[session.id]
    try:
        await asyncio.wait_for(started.wait(), 5)
        runtime.steer(session.id, "Skip the remaining files.", run)
    finally:
        release.set()
        await asyncio.wait_for(task, 5)
    executed_count = 4 if parallel else 1
    assert set(executed) == set(siblings[:executed_count])
    tool_responses = [message for message in requests[1] if message.role == "tool"]
    assert len(tool_responses) == len(siblings)
    assert all("superseded" in (item.content or "") for item in tool_responses[executed_count:])
    assert requests[1][-1].content == "Skip the remaining files."
    stored_calls = next(
        message["tool_outcomes"]
        for message in store.get_message_views(session.id)
        if message["tool_calls"]
    )
    skipped = stored_calls[tool_responses[executed_count].tool_call_id]
    assert skipped["status"] == "skipped"
    assert "superseded" in skipped["error"]
    finished = stored_calls[tool_responses[0].tool_call_id]
    assert finished["status"] == "ok"
    assert finished["duration_ms"] >= 0
    if not parallel:
        assert (sample_repo / siblings[0]).read_text(encoding="utf-8") == "new"
        assert (sample_repo / siblings[1]).read_text(encoding="utf-8") == "old"
        verification = [
            event.data["status"]
            for event in runtime.bus(session.id).history()
            if event.type == "verifier"
        ]
        assert verification[-2:] == ["running", "passed"]


async def test_late_correction_survives_reload_as_transcript_only(
    config, store, sample_repo, monkeypatch
):
    runtime = await make_runtime(config, store, sample_repo)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    finishing, release = asyncio.Event(), asyncio.Event()
    complete = runtime.loop._complete

    async def completing(*args, **kwargs):
        finishing.set()
        await release.wait()
        return await complete(*args, **kwargs)

    monkeypatch.setattr(runtime.loop, "_complete", completing)
    run, _ = runtime.start_run(session, "Start")
    task = runtime._tasks[session.id]
    try:
        await asyncio.wait_for(finishing.wait(), 5)
        message_id, _ = runtime.steer(session.id, "Late correction.", run)
    finally:
        release.set()
        await asyncio.wait_for(task, 5)
    dropped = [
        event for event in runtime.bus(session.id).history() if event.type == "steering.dropped"
    ]
    assert [event.data["message_id"] for event in dropped] == [message_id]
    app = create_app(config, runtime=runtime, token="test-token")
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/v1/sessions/{session.id}/messages", headers={"Authorization": "Bearer test-token"}
        )
    note = response.json()["messages"][-1]
    assert (note["id"], note["content"], note["delivery"]) == (
        message_id,
        "Late correction.",
        "unapplied",
    )
    assert all(
        item.content != "Late correction." for item in store.get_context_messages(session.id)
    )
    reopened = Store(store.path)
    try:
        assert reopened.get_message_views(session.id)[-1]["delivery"] == "unapplied"
    finally:
        reopened.close()


async def test_queue_keeps_its_order_and_retries_when_workspace_becomes_available(
    config, store, sample_repo, monkeypatch
):
    runtime = await make_runtime(config, store, sample_repo)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    monkeypatch.setattr("siwah.server.runtime.QUEUE_RETRY_DELAY_S", 0.01)
    blocker = asyncio.create_task(asyncio.sleep(30))
    runtime._tasks[session.id] = blocker
    first, _, _ = runtime.submit_run(session, "First in queue")
    second, _, _ = runtime.submit_run(session, "Second in queue")
    blocker.cancel()
    await asyncio.gather(blocker, return_exceptions=True)
    runtime._tasks.pop(session.id, None)
    with runtime._exclusive_operation(session.id):
        runtime._drain_queue(session.id)
        items = runtime.queue_snapshot(session.id)["items"]
        assert [item["id"] for item in items] == [first, second]
        assert "busy" in items[0]["blocked_reason"]
        assert "blocked_reason" not in items[1]
        assert session.id in runtime._queue_retries
        assert not runtime.is_running(session.id)
    for _ in range(200):
        if not runtime.queue_snapshot(session.id)["items"] and not runtime.is_running(session.id):
            break
        await asyncio.sleep(0.01)
    messages = store.get_messages(session.id)
    assert [item.content for item in messages if item.role == "user"] == [
        "First in queue",
        "Second in queue",
    ]
    assert not runtime._queue_retries


async def test_shutdown_does_not_start_queued_work(config, store, sample_repo, monkeypatch):
    runtime = await make_runtime(config, store, sample_repo)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    started = asyncio.Event()
    completed = asyncio.Event()
    provider_calls = 0

    async def stream(role, **kwargs):
        nonlocal provider_calls
        provider_calls += 1
        started.set()
        await completed.wait()

    monkeypatch.setattr(runtime.router, "stream", stream)
    runtime.start_run(session, "Active")
    await asyncio.wait_for(started.wait(), 5)
    queued, _, _ = runtime.submit_run(session, "Never dispatch on shutdown")
    await runtime.close()
    assert provider_calls == 1
    assert not runtime._tasks
    assert runtime.queue_snapshot(session.id)["items"][0]["id"] == queued


async def test_sessions_report_permission_waiters_for_background_tabs(config, store, sample_repo):
    runtime = await make_runtime(config, store, sample_repo)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    request = PermissionRequest(tool="bash", kind="bash", preview="pytest -q")
    wait = asyncio.create_task(
        runtime._resolve_permission(request, "approval", session_id=session.id)
    )
    runtime._tasks[session.id] = wait
    app = create_app(config, runtime=runtime, token="test-token")
    headers = {"Authorization": "Bearer test-token"}
    try:
        await asyncio.sleep(0)
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/v1/sessions", headers=headers)
            row = next(item for item in response.json()["sessions"] if item["id"] == session.id)
            assert row["status"] == "running"
            assert row["metadata"]["waiting_permission"] is True
            assert runtime.resolve_permission("approval", "once", session_id=session.id)
            await wait
            response = await client.get(f"/v1/sessions/{session.id}", headers=headers)
            assert "waiting_permission" not in response.json()["metadata"]
    finally:
        wait.cancel()
        await asyncio.gather(wait, return_exceptions=True)
