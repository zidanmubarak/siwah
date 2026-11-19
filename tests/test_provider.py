from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from siwah.auth import AuthStore
from siwah.config import AuthConfig, Config, ProviderConfig, RolesConfig
from siwah.core.types import ChatRequest, Message, ToolSpec
from siwah.providers.base import ProviderError
from siwah.providers.fixtures import FixtureTransport
from siwah.providers.openai_chat import OpenAIChatAdapter, _model_info, normalize_usage
from siwah.providers.registry import ProviderRegistry, RoleRouter
from siwah.providers.repair import ToolArgumentError, parse_tool_arguments
from siwah.version import __version__


def sse(*chunks: dict | str) -> str:
    lines = []
    for chunk in chunks:
        data = chunk if isinstance(chunk, str) else json.dumps(chunk)
        lines.append(f"data: {data}\n\n")
    lines.append("data: [DONE]\n\n")
    return "".join(lines)


def provider_config(**overrides) -> ProviderConfig:
    values = {
        "api": "openai_chat",
        "base_url": "https://mock.invalid/v1",
        "models_url": "https://mock.invalid/v1/models",
        "auth": AuthConfig(scheme="none"),
        "headers": {
            "User-Agent": "siwah/{version}",
            "x-opencode-session": "{session_id}",
        },
    }
    values.update(overrides)
    return ProviderConfig(**values)


def test_openai_chat_streams_text_reasoning_tool_and_allowance(tmp_path: Path):
    seen: list[httpx.Request] = []
    body = sse(
        {"choices": [{"delta": {"role": "assistant", "content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {"choices": [{"delta": {"reasoning_content": "think"}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {"name": "read_file", "arguments": '{"path"'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ': "math.py"}'}}]}}
            ]
        },
        {
            "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            "usage": {
                "prompt_tokens": 361,
                "completion_tokens": 69,
                "total_tokens": 430,
                "prompt_tokens_details": {"cached_tokens": 256},
                "completion_tokens_details": {"reasoning_tokens": 11},
                "cost": 0,
                "opencode": {
                    "billing_source": "go",
                    "list_cost": 0.00009555,
                    "allowance": 0.00005733,
                    "tokens": {
                        "input": 361,
                        "output": 69,
                        "reasoning": 11,
                        "cache_read": 256,
                        "cache_write_5m": 0,
                        "cache_write_1h": 0,
                    },
                },
            },
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    adapter = OpenAIChatAdapter(
        "opencode-go",
        provider_config(),
        AuthStore(tmp_path / "auth.json"),
        transport=httpx.MockTransport(handler),
    )
    events = []

    async def collect(event):
        events.append(event)

    import asyncio

    outcome = asyncio.run(
        adapter.stream_chat(
            ChatRequest(
                model="model-a",
                messages=[Message(role="user", content="hi")],
                tools=[
                    ToolSpec(
                        name="read_file",
                        description="read",
                        parameters={"type": "object", "properties": {}},
                    )
                ],
            ),
            session_id="ses_fixture",
            emit=collect,
        )
    )
    asyncio.run(adapter.aclose())

    request = seen[0]
    assert request.headers["user-agent"] == f"siwah/{__version__}"
    assert request.headers["x-opencode-session"] == "ses_fixture"
    payload = json.loads(request.content)
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["tools"][0]["function"]["name"] == "read_file"

    assert outcome.text == "Hello"
    assert outcome.reasoning == "think"
    assert outcome.tool_calls[0].name == "read_file"
    assert outcome.tool_calls[0].arguments == {"path": "math.py"}
    assert outcome.usage.allowance == pytest.approx(0.00005733)
    assert outcome.usage.cache_read_tokens == 256
    assert outcome.usage.reasoning_tokens == 11
    assert outcome.usage.billing_source == "go"
    assert any(event.type == "usage" for event in events)


def test_normalize_usage_missing_fields():
    usage = normalize_usage({"prompt_tokens": 2}, "usage.opencode.allowance")
    assert usage.input_tokens == 2
    assert usage.allowance == 0
    assert usage.cache_read_tokens == 0


def test_http_error_shape():
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            400, json={"error": {"type": "server_error", "message": "Model is unavailable."}}
        )
    )
    adapter = OpenAIChatAdapter("mock", provider_config(), AuthStore(), transport=transport)

    import asyncio

    with pytest.raises(ProviderError) as error:
        asyncio.run(
            adapter.stream_chat(
                ChatRequest(model="bad", messages=[Message(role="user", content="hi")]),
                session_id="ses",
                emit=_noop,
            )
        )
    asyncio.run(adapter.aclose())
    assert error.value.status == 400
    assert error.value.retryable is False


async def _noop(_event=None) -> None:
    return None


def test_repair_arguments():
    assert parse_tool_arguments('```json\n{"path": "a.py",}\n```') == {"path": "a.py"}
    with pytest.raises(ToolArgumentError):
        parse_tool_arguments('{"path": ')


def test_record_and_replay(tmp_path: Path):
    body = sse(
        {"choices": [{"delta": {"content": "recorded"}}]},
        {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=body
        )
    )
    config = provider_config()
    record = tmp_path / "fixture.json"
    adapter = OpenAIChatAdapter(
        "mock", config, AuthStore(), transport=transport, record_path=record
    )

    import asyncio

    request = ChatRequest(model="model-a", messages=[Message(role="user", content="hi")])
    asyncio.run(adapter.stream_chat(request, session_id="ses", emit=_noop))
    asyncio.run(adapter.aclose())
    assert record.exists()

    replay = OpenAIChatAdapter("mock", config, AuthStore(), transport=FixtureTransport(record))
    outcome = asyncio.run(replay.stream_chat(request, session_id="ses", emit=_noop))
    asyncio.run(replay.aclose())
    assert outcome.text == "recorded"


def test_router_falls_back_on_retryable_error(tmp_path: Path):
    config = Config(
        providers={
            "primary": provider_config(max_retries=0),
            "backup": provider_config(max_retries=0),
        },
        roles=RolesConfig(main="primary/model-a", fallback=["backup/model-b"]),
    )
    registry = ProviderRegistry(config, AuthStore(tmp_path / "auth.json"))
    calls: list[str] = []

    from .conftest import ScriptedAdapter, outcome

    class FailingAdapter(ScriptedAdapter):
        async def stream_chat(self, request, *, session_id, emit):
            calls.append(request.model)
            raise ProviderError("rate limited", code="provider_rate_limited", retryable=True)

    primary = FailingAdapter([], config.providers["primary"])
    backup = ScriptedAdapter([outcome(text="from backup")], config.providers["backup"])
    registry._adapters["primary"] = primary  # type: ignore[assignment]
    registry._adapters["backup"] = backup  # type: ignore[assignment]
    router = RoleRouter(config, registry)

    async def emit(_event):
        return None

    import asyncio

    result = asyncio.run(
        router.stream(
            "main",
            messages=[Message(role="user", content="hi")],
            tools=[],
            session_id="ses",
            emit=emit,
        )
    )
    assert result.text == "from backup"
    assert calls == ["model-a"]


def test_model_info_reads_provider_effort_values():
    info = _model_info(
        {
            "id": "model-max",
            "limit": {"context": 1000, "output": 100},
            "capabilities": {
                "tools": {"supported": True},
                "reasoning": {"supported": True, "effort": ["low", "high", "max"]},
            },
        }
    )

    assert info.efforts == ["low", "high", "max"]
    assert info.supports_reasoning is True
