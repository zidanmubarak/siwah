from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from siwah.auth import AuthStore
from siwah.config import AgentConfig, EscalationConfig, VerifyConfig
from siwah.core.loop import AgentLoop
from siwah.core.storage import Store
from siwah.core.types import (
    ChatRequest,
    ModelInfo,
    StreamOutcome,
    Usage,
)
from siwah.providers.base import EventSink, ProviderAdapter, ProviderError
from siwah.providers.registry import ProviderRegistry, RoleRouter
from siwah.tools.base import ToolRegistry
from siwah.tools.runtime import default_tools


class FailingAdapter(ProviderAdapter):
    provider_name = "mock"
    api = "openai_chat"

    def __init__(self, config) -> None:
        self.config = config
        self.models: list[str] = []

    async def list_models(self) -> list[ModelInfo]:
        return []

    async def stream_chat(
        self, request: ChatRequest, *, session_id: str, emit: EventSink
    ) -> StreamOutcome:
        self.models.append(request.model)
        if len(self.models) == 1:
            raise ProviderError("temporary", code="server_error", retryable=False, status=503)
        usage = Usage(input_tokens=10, output_tokens=2)
        return StreamOutcome(
            provider="mock",
            model=request.model,
            text="recovered",
            usage=usage,
            finish_reason="stop",
        )

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_escalation_switches_to_the_configured_model(config, store: Store, sample_repo: Path):
    config = config.model_copy(
        update={
            "escalation": EscalationConfig(enabled=True, after_failures=1, model="mock/model-b"),
            "agent": AgentConfig(max_steps=3),
            "verify": VerifyConfig(auto=False),
        }
    )
    adapter = FailingAdapter(config.providers["mock"])
    registry = ProviderRegistry(config, AuthStore())
    registry._adapters["mock"] = adapter  # type: ignore[assignment]
    router = RoleRouter(config, registry)
    loop = AgentLoop(
        config=config,
        store=store,
        router=router,
        tools=ToolRegistry(default_tools()),
    )
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(event: str, data: dict) -> None:
        events.append((event, data))

    result = await loop.run(session, "do it", emit=emit)
    await registry.aclose()

    assert result.text == "recovered"
    assert adapter.models == ["model-a", "model-b"]
    assert any(event == "escalation" for event, _ in events)
