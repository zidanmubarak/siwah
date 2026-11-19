"""Deterministic mock model and the naive tool set for eval runs."""

from __future__ import annotations

from ..config import ProviderConfig
from ..core.types import (
    ChatRequest,
    ModelInfo,
    ProviderEvent,
    StreamOutcome,
    ToolCall,
    Usage,
)
from ..providers.base import EventSink, ProviderAdapter
from ..tools.base import Tool
from .models import EvalTask, MockAction

# Deterministic evaluation harness identifiers. These are not user-facing model
# defaults: the CLI only selects them for an explicit mock eval run, and a live
# run requires a real model id.
MOCK_PROVIDER = "mock"
MOCK_MODEL_ID = "model-a"
MOCK_MODEL = f"{MOCK_PROVIDER}/{MOCK_MODEL_ID}"
MOCK_BASE_URL = "http://mock.invalid/v1"
MOCK_MODELS_URL = "http://mock.invalid/v1/models"


class MockEvalAdapter(ProviderAdapter):
    """A deterministic model driven by the task's mock action script.

    The same adapter is used by the siwah and naive harnesses. It repairs only
    when a verifier failure is present in the conversation, so a harness without
    verifier feedback gets the same first action and no repair.
    """

    api = "openai_chat"
    provider_name = MOCK_PROVIDER

    def __init__(self, task: EvalTask, config: ProviderConfig) -> None:
        self.task = task
        self.config = config
        self.first_sent = False
        self.repair_sent = False
        self.calls = 0

    async def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(
                id=MOCK_MODEL_ID,
                name="Eval Model A",
                context_window=100_000,
                max_output=8_000,
                supports_tools=True,
                supports_reasoning=True,
            )
        ]

    async def stream_chat(
        self, request: ChatRequest, *, session_id: str, emit: EventSink
    ) -> StreamOutcome:
        self.calls += 1
        tool_messages = [message for message in request.messages if message.role == "tool"]
        verifier_failed = any(
            "Verifier: failed" in (message.content or "") for message in tool_messages
        )
        actions: list[MockAction] = []
        if not self.first_sent and self.task.mock.first_tools:
            actions = list(self.task.mock.first_tools)
            self.first_sent = True
        elif (
            self.task.mock.requires_verifier
            and verifier_failed
            and not self.repair_sent
            and self.task.mock.repair_tools
        ):
            actions = list(self.task.mock.repair_tools)
            self.repair_sent = True

        usage = Usage(
            input_tokens=1_000,
            output_tokens=50,
            cache_read_tokens=800,
            allowance=0.0001,
            billing_source="go",
        )
        if actions:
            calls = [
                ToolCall(
                    id=f"eval_{self.task.id}_{self.calls}_{index}",
                    name=action.name,
                    arguments=dict(action.arguments),
                )
                for index, action in enumerate(actions)
            ]
            await emit(ProviderEvent(type="usage", usage=usage))
            await emit(ProviderEvent(type="finish", finish_reason="tool_calls"))
            return StreamOutcome(
                provider=MOCK_PROVIDER,
                model=MOCK_MODEL_ID,
                tool_calls=calls,
                usage=usage,
                finish_reason="tool_calls",
            )

        text = self.task.mock.final
        for index in range(0, len(text), 3):
            await emit(ProviderEvent(type="text.delta", text=text[index : index + 3]))
        await emit(ProviderEvent(type="usage", usage=usage))
        await emit(ProviderEvent(type="finish", finish_reason="stop"))
        return StreamOutcome(
            provider=MOCK_PROVIDER,
            model=MOCK_MODEL_ID,
            text=text,
            usage=usage,
            finish_reason="stop",
        )

    async def aclose(self) -> None:
        return None


def basic_tools() -> list[Tool]:
    """The fixed naive baseline: MVP tools only, without advanced harness features."""

    from ..tools.fs import EditFileTool, GlobTool, GrepTool, ReadFileTool, WriteFileTool
    from ..tools.runtime import BashTool, TodoTool

    return [
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        GlobTool(),
        GrepTool(),
        BashTool(),
        TodoTool(),
    ]


__all__ = ["MockEvalAdapter", "basic_tools"]
