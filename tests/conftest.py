"""Reusable fixtures for the siwah test suite."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from siwah.auth import AuthStore
from siwah.config import (
    AgentConfig,
    AuthConfig,
    CheckpointConfig,
    Config,
    PermissionsConfig,
    ProviderConfig,
    RolesConfig,
    VerifyConfig,
)
from siwah.core.loop import AgentLoop
from siwah.core.storage import Store
from siwah.core.types import (
    ChatRequest,
    ModelInfo,
    ProviderEvent,
    StreamOutcome,
    ToolCall,
    Usage,
)
from siwah.paths import ensure_home
from siwah.providers.base import ProviderAdapter
from siwah.providers.registry import ProviderRegistry, RoleRouter
from siwah.tools.base import ToolRegistry
from siwah.tools.runtime import default_tools


class ScriptedAdapter(ProviderAdapter):
    api = "openai_chat"

    def __init__(self, outcomes: list[StreamOutcome], config: ProviderConfig) -> None:
        self.provider_name = "mock"
        self.config = config
        self.outcomes = list(outcomes)
        self.requests: list[ChatRequest] = []

    async def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(
                id="model-a",
                name="Model A",
                context_window=100_000,
                max_output=8_000,
                supports_tools=True,
                supports_reasoning=True,
                reasoning_field="reasoning_content",
            )
        ]

    async def stream_chat(self, request: ChatRequest, *, session_id: str, emit) -> StreamOutcome:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if outcome.text:
            for index in range(0, len(outcome.text), 3):
                await emit(ProviderEvent(type="text.delta", text=outcome.text[index : index + 3]))
        if outcome.reasoning:
            await emit(ProviderEvent(type="reasoning.delta", text=outcome.reasoning))
        await emit(ProviderEvent(type="usage", usage=outcome.usage))
        await emit(ProviderEvent(type="finish", finish_reason=outcome.finish_reason))
        return outcome

    async def aclose(self) -> None:
        return None


def outcome(
    *,
    text: str = "",
    reasoning: str = "",
    tool_calls: list[Any] | None = None,
    allowance: float = 0.0,
) -> StreamOutcome:
    return StreamOutcome(
        provider="mock",
        model="model-a",
        text=text,
        reasoning=reasoning,
        tool_calls=tool_calls or [],
        usage=Usage(
            input_tokens=10,
            output_tokens=5,
            allowance=allowance,
            billing_source="go",
        ),
        finish_reason="stop" if not tool_calls else "tool_calls",
    )


def tool_call(name: str, arguments: dict[str, Any], call_id: str = "call_1"):
    return ToolCall(id=call_id, name=name, arguments=arguments)


@pytest.fixture(autouse=True)
def _disable_auto_evolution(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SIWAH_EVOLVE_AUTO", "0")
    monkeypatch.setenv("SIWAH_CURATOR", "0")


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "math_utils.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_math_utils.py").write_text(
        "from math_utils import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("# sample\n", encoding="utf-8")
    return repo


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        providers={
            "mock": ProviderConfig(
                api="openai_chat",
                base_url="https://mock.invalid/v1",
                models_url="https://mock.invalid/v1/models",
                auth=AuthConfig(scheme="none"),
                headers={
                    "User-Agent": "siwah/{version}",
                    "x-opencode-session": "{session_id}",
                },
                max_retries=0,
            )
        },
        roles=RolesConfig(main="mock/model-a"),
        permissions=PermissionsConfig(mode="auto-edit"),
        agent=AgentConfig(max_steps=10, tool_output_limit=10_000, bash_timeout_s=30),
        verify=VerifyConfig(auto=False),
        checkpoints=CheckpointConfig(enabled=False),
    )


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Store]:
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    ensure_home()
    database = Store(tmp_path / "state.db")
    yield database
    database.close()


def make_loop(config: Config, store: Store, adapter: ScriptedAdapter) -> AgentLoop:
    registry = ProviderRegistry(config, AuthStore())
    registry._adapters["mock"] = adapter
    router = RoleRouter(config, registry)
    return AgentLoop(
        config=config,
        store=store,
        router=router,
        tools=ToolRegistry(default_tools()),
    )


def python_executable() -> str:
    return sys.executable
