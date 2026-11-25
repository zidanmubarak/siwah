"""Shared domain types for the agent loop and provider adapters."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SUPERSEDED_TOOL_MESSAGE = "Tool call superseded by a user correction."


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    raw_arguments: str | None = None
    arguments_valid: bool = True


class ToolSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class MessageAttachment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mime: str
    data: str
    name: str | None = None

    def data_url(self) -> str:
        return f"data:{self.mime};base64,{self.data}"


class Message(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    reasoning: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    attachments: list[MessageAttachment] = Field(default_factory=list)

    def to_openai(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": self.role}
        images = [item for item in self.attachments if item.mime.startswith("image/")]
        if self.role == "user" and images:
            payload["content"] = [
                {"type": "text", "text": self.content or ""},
                *[{"type": "image_url", "image_url": {"url": item.data_url()}} for item in images],
            ]
        else:
            payload["content"] = self.content or ""
        if self.role == "assistant" and self.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.raw_arguments
                        if call.raw_arguments is not None
                        else _json_arguments(call.arguments),
                    },
                }
                for call in self.tool_calls
            ]
        if self.role == "tool":
            payload["tool_call_id"] = self.tool_call_id
        return payload


class Usage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    allowance: float = 0.0
    cost: float = 0.0
    allowance_reported: bool = False
    cost_reported: bool = False
    list_cost: float = 0.0
    billing_source: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str
    messages: list[Message]
    tools: list[ToolSpec] = Field(default_factory=list)
    temperature: float | None = None
    max_tokens: int | None = None
    effort: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class RetryNotice(BaseModel):
    """A retry that will happen before the next provider attempt."""

    provider: str
    model: str
    attempt: int
    max_attempts: int
    delay_s: float
    code: str
    message: str


class ProviderEvent(BaseModel):
    """A normalized streaming event emitted by an adapter."""

    model_config = ConfigDict(extra="ignore")

    type: Literal[
        "text.delta", "reasoning.delta", "tool_call.delta", "usage", "finish", "retry", "reset"
    ]
    text: str | None = None
    index: int | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments_delta: str | None = None
    usage: Usage | None = None
    finish_reason: str | None = None
    retry: RetryNotice | None = None


class StreamOutcome(BaseModel):
    model_config = ConfigDict(extra="ignore")

    provider: str
    model: str
    text: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    finish_reason: str | None = None
    raw_assistant: dict[str, Any] = Field(default_factory=dict)


class ModelInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str | None = None
    context_window: int | None = None
    max_output: int | None = None
    supports_tools: bool | None = None
    supports_reasoning: bool | None = None
    efforts: list[str] = Field(default_factory=list)
    reasoning_field: str | None = None
    native_api: str | None = None
    pricing: dict[str, Any] = Field(default_factory=dict)
    privacy: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)


def _json_arguments(arguments: dict[str, Any]) -> str:
    import json

    return json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
