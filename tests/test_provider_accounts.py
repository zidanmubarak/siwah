from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from siwah.auth import AuthStore
from siwah.config import AuthConfig, Config, ConfigError, ProviderConfig, add_default_providers
from siwah.core.storage import Store
from siwah.core.types import ChatRequest, Message, ToolCall, ToolSpec
from siwah.providers.base import ProviderError
from siwah.providers.openai_chat import OpenAIChatAdapter, normalize_usage
from siwah.providers.openai_responses import OpenAIResponsesAdapter
from siwah.providers.openrouter_auth import OpenRouterLogin, supports_browser_login
from siwah.server.app import create_app
from siwah.server.runtime import DaemonRuntime


async def _ignore(_event):
    pass


async def _token():
    return "fixture-access-token"


def test_add_presets_preserves_custom_provider_tables_and_inline_configs(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SIWAH_CONFIG", str(path))
    text = "# Custom provider\n[providers.custom]\nbase_url = 'https://example.test'\n"
    path.write_text(text, encoding="utf-8")
    config = add_default_providers()
    assert config.providers["custom"].base_url == "https://example.test"
    assert config.providers["chatgpt"].api == "openai_responses"
    assert path.read_text(encoding="utf-8").startswith(text)

    sealed = "providers = { custom = { base_url = 'https://example.test' } }\n"
    path.write_text(sealed, encoding="utf-8")
    with pytest.raises(ConfigError, match="separate provider tables"):
        add_default_providers()
    assert path.read_text(encoding="utf-8") == sealed


@pytest.mark.asyncio
async def test_openrouter_browser_exchanges_once_with_matching_pkce_and_fixed_origin():
    flow = OpenRouterLogin()
    authorization = urlsplit(flow.authorization_url())
    query = parse_qs(authorization.query)
    assert authorization.netloc == "openrouter.ai"
    assert query["code_challenge_method"] == ["S256"]

    def exchange(request):
        assert str(request.url) == "https://openrouter.ai/api/v1/auth/keys"
        body = json.loads(request.content)
        assert body["code"] == "one-time-code"
        digest = hashlib.sha256(body["code_verifier"].encode()).digest()
        assert query["code_challenge"] == [base64.urlsafe_b64encode(digest).decode().rstrip("=")]
        return httpx.Response(200, json={"key": "test-router-key"})

    assert (
        await flow.exchange("one-time-code", transport=httpx.MockTransport(exchange))
        == "test-router-key"
    )
    assert not supports_browser_login(
        ProviderConfig(base_url="https://unrelated.test", auth=AuthConfig(browser="openrouter"))
    )


@pytest.mark.asyncio
async def test_daemon_connects_openrouter_one_time_flow_without_returning_credentials(
    tmp_path, monkeypatch
):
    async def exchange(_flow, code):
        assert code == "browser-code"
        return "fixture-secret"

    monkeypatch.setattr(OpenRouterLogin, "exchange", exchange)
    monkeypatch.setattr("siwah.server.app.webbrowser.open", lambda _url: True)
    config = Config(
        providers={
            "openrouter": ProviderConfig(
                base_url="https://openrouter.ai/api/v1",
                auth=AuthConfig(browser="openrouter"),
            ),
        }
    )
    auth = AuthStore(tmp_path / "auth.json")
    runtime = DaemonRuntime(config, store=Store(tmp_path / "state.db"), auth=auth)
    transport = httpx.ASGITransport(app=create_app(config, runtime=runtime, token="test-token"))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            start = "/v1/auth/browser/openrouter"
            assert (await client.post(start)).status_code == 401
            headers = {"Authorization": "Bearer test-token"}
            started = await client.post(start, headers=headers)
            assert started.status_code == 200
            assert "code_verifier" not in started.text
            request = {"flow_id": started.json()["flow_id"], "code": "browser-code"}
            completed = await client.post(f"{start}/complete", headers=headers, json=request)
            assert completed.status_code == 200
            assert "fixture-secret" not in completed.text
            assert auth.get_key("openrouter", AuthConfig()) == "fixture-secret"
            replayed = await client.post(f"{start}/complete", headers=headers, json=request)
            assert replayed.status_code == 400
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_chatgpt_plan_lists_models_and_streams_tools_with_provider_usage():
    tool = {
        "type": "function_call",
        "namespace": "siwah",
        "name": "read_file",
        "call_id": "call_1",
        "arguments": '{"path":"README.md"}',
    }

    def api(request):
        assert request.headers["Authorization"] == "Bearer fixture-access-token"
        assert request.url.host == "api.openai.com"
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"slug": "hidden-model", "visibility": "hidden"},
                        {
                            "slug": "plan-model",
                            "visibility": "list",
                            "display_name": "Plan model",
                            "context_window": 10000,
                            "supported_reasoning_levels": [{"effort": "high"}],
                        },
                    ]
                },
            )
        assert request.url.path == "/v1/responses"
        payload = json.loads(request.content)
        assert payload["store"] is False and payload["stream"] is True
        assert not {"max_output_tokens", "previous_response_id", "temperature"}.intersection(
            payload
        )
        assert payload["reasoning"] == {"effort": "high"}
        assert payload["input"][0]["role"] == "developer"
        assert payload["input"][1]["namespace"] == "siwah"
        assert payload["input"][2]["type"] == "function_call_output"
        assert payload["tools"][0]["name"] == "siwah"
        assert payload["tools"][0]["tools"][0]["name"] == "read_file"
        return _sse(
            {"type": "response.output_text.delta", "delta": "Reading."},
            {"type": "response.output_item.done", "output_index": 0, "item": tool},
            {
                "type": "response.completed",
                "response": {
                    "output": [tool],
                    "usage": {
                        "input_tokens": 95,
                        "output_tokens": 47,
                        "input_tokens_details": {"cached_tokens": 55},
                        "output_tokens_details": {"reasoning_tokens": 23},
                    },
                },
            },
        )

    adapter = OpenAIResponsesAdapter(
        "chatgpt",
        ProviderConfig(base_url="https://api.openai.com/v1"),
        _token,
        transport=httpx.MockTransport(api),
    )
    try:
        models = await adapter.list_models()
        assert [
            (model.id, model.name, model.context_window, model.efforts) for model in models
        ] == [
            ("plan-model", "Plan model", 10000, ["high"]),
        ]
        result = await adapter.stream_chat(
            ChatRequest(
                model="plan-model",
                temperature=0,
                max_tokens=10,
                effort="high",
                messages=[
                    Message(role="system", content="Use tools."),
                    Message(
                        role="assistant", tool_calls=[ToolCall(id="old-call", name="read_file")]
                    ),
                    Message(role="tool", tool_call_id="old-call", content="Result."),
                    Message(role="user", content="Continue."),
                ],
                tools=[ToolSpec(name="read_file", description="Read a file.")],
            ),
            session_id="test",
            emit=_ignore,
        )
    finally:
        await adapter.aclose()
    assert result.text == "Reading."
    assert result.tool_calls[0].arguments == {"path": "README.md"}
    assert result.usage.input_tokens == 95
    assert result.usage.output_tokens == 47
    assert result.usage.reasoning_tokens == 23
    assert result.usage.cache_read_tokens == 55
    assert not result.usage.cost_reported


@pytest.mark.asyncio
async def test_chatgpt_stream_limit_offers_usage_management_without_retry():
    def api(_request):
        return _sse(
            {"type": "response.output_text.delta", "delta": "Partial text"},
            {
                "type": "response.failed",
                "response": {
                    "error": {
                        "code": "subscription_sharing_usage_limit_exceeded",
                    }
                },
            },
        )

    adapter = OpenAIResponsesAdapter(
        "chatgpt",
        ProviderConfig(base_url="https://api.openai.com/v1"),
        _token,
        transport=httpx.MockTransport(api),
    )
    try:
        with pytest.raises(ProviderError, match="https://chatgpt.com/settings/usage") as error:
            await adapter.stream_chat(
                ChatRequest(model="plan-model", messages=[]), session_id="test", emit=_ignore
            )
        assert error.value.code == "subscription_sharing_usage_limit_exceeded"
        assert not error.value.retryable
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_chat_catalog_filters_other_protocols_and_reads_direct_model_limits(tmp_path):
    def api(_request):
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "messages-only", "supported_endpoints": ["/messages"]},
                    {
                        "id": "direct-model",
                        "supported_endpoints": ["/chat/completions", "/responses"],
                        "context_window": 1048576,
                        "max_output_tokens": 393216,
                        "effort": {
                            "supported_levels": ["low", "high", "max"],
                            "default_level": "high",
                        },
                    },
                    {"id": "versioned-model", "supported_endpoints": ["/v1/chat/completions"]},
                ]
            },
        )

    adapter = OpenAIChatAdapter(
        "direct",
        ProviderConfig(
            base_url="https://example.test",
            models_url="https://example.test/models",
            auth=AuthConfig(scheme="none"),
        ),
        AuthStore(tmp_path / "auth.json"),
        transport=httpx.MockTransport(api),
    )
    try:
        models = await adapter.list_models()
        assert [
            (model.id, model.context_window, model.max_output, model.efforts) for model in models
        ] == [
            ("direct-model", 1048576, 393216, ["low", "high", "max"]),
            ("versioned-model", None, None, []),
        ]
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_direct_deepseek_replays_thinking_and_ignores_unsupported_effort(tmp_path):
    def api(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "fixture-model", "effort": {"supported_levels": ["low", "high"]}}
                    ]
                },
            )
        payload = json.loads(request.content)
        assert payload["messages"][0]["reasoning_content"] == "Prior provider thinking."
        assert "reasoning_effort" not in payload
        return _sse({"choices": [{"delta": {"content": "Done"}, "finish_reason": "stop"}]})

    adapter = OpenAIChatAdapter(
        "deepseek",
        ProviderConfig(
            base_url="https://api.deepseek.com",
            models_url="https://api.deepseek.com/models",
            auth=AuthConfig(scheme="none"),
            replay_reasoning=True,
        ),
        AuthStore(tmp_path / "auth.json"),
        transport=httpx.MockTransport(api),
    )
    try:
        await adapter.list_models()
        outcome = await adapter.stream_chat(
            ChatRequest(
                model="fixture-model",
                effort="medium",
                messages=[
                    Message(
                        role="assistant",
                        content="First turn.",
                        reasoning="Prior provider thinking.",
                    )
                ],
                tools=[ToolSpec(name="read_file", description="Read a file.")],
            ),
            session_id="test",
            emit=_ignore,
        )
        assert outcome.text == "Done"
    finally:
        await adapter.aclose()


def test_reported_zero_cost_and_deepseek_cache_tokens_are_preserved():
    usage = normalize_usage(
        {"prompt_tokens": 17, "prompt_cache_hit_tokens": 13, "cost": 0},
        "usage.opencode.allowance",
    )
    assert usage.cache_read_tokens == 13
    assert usage.cost == 0 and usage.cost_reported
    assert not usage.allowance_reported


def _sse(*events):
    return httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream"},
        text="".join(f"data: {json.dumps(event)}\n\n" for event in events),
    )
