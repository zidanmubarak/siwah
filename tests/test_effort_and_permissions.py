from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from siwah.auth import AuthStore
from siwah.config import Config
from siwah.core.storage import Store
from siwah.core.types import ChatRequest, Message, ProviderEvent, StreamOutcome
from siwah.providers.openai_chat import _payload
from siwah.providers.registry import ProviderRegistry, RoleRouter
from siwah.server.app import create_app
from siwah.server.runtime import DaemonRuntime


def test_reasoning_effort_is_forwarded_to_the_openai_payload():
    request = ChatRequest(
        model="model-a",
        messages=[Message(role="user", content="hi")],
        effort="high",
    )

    payload = _payload(request, True)

    assert payload["reasoning_effort"] == "high"
    assert (
        _payload(request.model_copy(update={"effort": None}), True).get("reasoning_effort") is None
    )


class EffortAdapter:
    api = "openai_chat"
    provider_name = "mock"

    def __init__(self) -> None:
        self.config = None
        self.effort: str | None = None

    async def stream_chat(self, request: ChatRequest, *, session_id: str, emit) -> StreamOutcome:
        self.effort = request.effort
        await emit(ProviderEvent(type="text.delta", text="ok"))
        return StreamOutcome(provider="mock", model=request.model, text="ok")


@pytest.mark.asyncio
async def test_router_passes_effort(config):
    adapter = EffortAdapter()
    adapter.config = config.providers["mock"]  # type: ignore[assignment]
    registry = ProviderRegistry(config, AuthStore())
    registry._adapters["mock"] = adapter  # type: ignore[assignment]
    router = RoleRouter(config, registry)

    async def emit(_event) -> None:
        return None

    await router.stream(
        "main",
        messages=[Message(role="user", content="hi")],
        tools=[],
        session_id="s",
        emit=emit,
        effort="low",
    )

    assert adapter.effort == "low"


def test_permission_mode_and_session_effort_are_runtime_settings(tmp_path):
    store = Store(tmp_path / "state.db")
    runtime = DaemonRuntime(Config(), store=store)
    session = runtime.create_session(cwd=str(tmp_path), model="mock/model-a")

    assert runtime.permission_mode() in {"ask", "auto-edit", "yolo"}
    assert runtime.set_permission_mode("yolo") == "yolo"
    assert runtime.permission_mode() == "yolo"
    assert runtime.loop.config.permissions.mode == "yolo"

    updated = runtime.update_session(session.id, effort="medium")
    assert updated is not None
    assert updated.metadata["effort"] == "medium"

    cleared = runtime.update_session(session.id, effort="off")
    assert cleared is not None
    assert "effort" not in cleared.metadata

    with pytest.raises(ValueError):
        runtime.set_permission_mode("unsafe")
    store.close()


@pytest.mark.asyncio
async def test_http_exposes_effort_and_permission_mode(config, store, tmp_path):
    runtime = DaemonRuntime(config, store=store)
    app = create_app(config, runtime=runtime, token="test-token")
    headers = {"Authorization": "Bearer test-token"}
    transport = ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/sessions",
            headers=headers,
            json={"cwd": str(tmp_path), "model": "mock/model-a"},
        )
        session_id = created.json()["id"]

        patched = await client.patch(
            f"/v1/sessions/{session_id}", headers=headers, json={"effort": "high"}
        )
        assert patched.status_code == 200
        assert patched.json()["metadata"]["effort"] == "high"

        mode = await client.post("/v1/permissions/mode", headers=headers, json={"mode": "yolo"})
        assert mode.status_code == 200
        assert mode.json()["mode"] == "yolo"
        assert (await client.get("/v1/permissions/mode", headers=headers)).json()["mode"] == "yolo"

        rejected = await client.patch(
            f"/v1/sessions/{session_id}", headers=headers, json={"effort": "maximum"}
        )
        assert rejected.status_code == 422
