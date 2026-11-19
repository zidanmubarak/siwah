"""Mock-only regressions for provider error, routing, and protocol boundaries."""

from __future__ import annotations

import asyncio
import json
import math
import traceback
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from siwah.auth import AuthStore
from siwah.config import AuthConfig, Config, RolesConfig
from siwah.core.loop import AgentLoop
from siwah.core.storage import Store
from siwah.core.types import (
    ChatRequest,
    Message,
    ProviderEvent,
    StreamOutcome,
    ToolCall,
    ToolSpec,
)
from siwah.providers.base import ProviderError
from siwah.providers.openai_chat import OpenAIChatAdapter, _retry_after
from siwah.providers.profiles import (
    ModelProfile,
    ModelQuirks,
    PrivacyProfile,
    profile_path,
    save_profile,
)
from siwah.providers.registry import ProviderRegistry, RoleRouter
from siwah.tools.base import ToolRegistry
from siwah.tools.runtime import default_tools

from .test_provider import provider_config, sse

SYNTHETIC_KEY = "audit-only-credential-0123456789ABCDEFGHIJ"
READ_TOOL = ToolSpec(
    name="read_file",
    description="Read a file.",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))


async def noop(_event: ProviderEvent) -> None:
    return None


def success(text: str = "ok") -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text=sse({"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}),
    )


def credential_config(kind: str, monkeypatch: pytest.MonkeyPatch):
    if kind == "bearer":
        monkeypatch.setenv("SIWAH_TEST_PROVIDER_KEY", SYNTHETIC_KEY)
        return provider_config(
            auth=AuthConfig(scheme="bearer", env="SIWAH_TEST_PROVIDER_KEY"),
            max_retries=0,
        )
    return provider_config(headers={kind: SYNTHETIC_KEY}, max_retries=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["bearer", "X-Custom-API-Key", "X-Gateway-Access"])
@pytest.mark.parametrize("response_kind", ["http", "stream"])
async def test_echoed_credentials_never_reach_loop_errors(
    config: Config,
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    response_kind: str,
) -> None:
    config.providers["mock"] = credential_config(kind, monkeypatch)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        header = request.headers["Authorization" if kind == "bearer" else kind]
        assert SYNTHETIC_KEY in header
        error = {"error": {"message": f"Rejected credential: {header}"}}
        if response_kind == "http":
            return httpx.Response(401, json=error)
        return httpx.Response(200, text=sse(error))

    registry = ProviderRegistry(
        config, AuthStore(tmp_path / "auth.json"), transport=httpx.MockTransport(handler)
    )
    loop = AgentLoop(
        config=config,
        store=store,
        router=RoleRouter(config, registry),
        tools=ToolRegistry(default_tools()),
    )
    session = store.create_session(cwd=str(tmp_path), model="mock/model-a")
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(event_type: str, data: dict[str, Any]) -> None:
        events.append((event_type, data))

    try:
        result = await loop.run(session, "Say hello.", emit=emit)
    finally:
        await loop.close_jobs()
        await registry.aclose()
    assert len(seen) == 1
    assert result.error is not None
    error_event = next(data for event_type, data in events if event_type == "error")
    end_event = next(data for event_type, data in events if event_type == "run.end")
    for value in (result.error, error_event["message"], end_event["error"]):
        assert SYNTHETIC_KEY not in value
        assert "[REDACTED]" in value


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http", "stream", "malformed", "transport", "models"])
async def test_adapter_errors_redact_body_and_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    config = credential_config("bearer", monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "transport":
            raise httpx.ConnectError(f"Rejected {SYNTHETIC_KEY}", request=request)
        if failure in {"http", "models"}:
            return httpx.Response(401, text=f"Rejected {SYNTHETIC_KEY}")
        if failure == "malformed":
            return httpx.Response(200, text=f"data: not-json {SYNTHETIC_KEY}\n\n")
        return httpx.Response(200, text=sse({"error": {"message": f"Rejected {SYNTHETIC_KEY}"}}))

    adapter = OpenAIChatAdapter(
        "mock", config, AuthStore(tmp_path / "auth.json"), transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(ProviderError) as caught:
            if failure == "models":
                await adapter.list_models()
            else:
                await adapter.stream_chat(
                    ChatRequest(model="model-a", messages=[]), session_id="ses", emit=noop
                )
    finally:
        await adapter.aclose()
    error = caught.value
    assert SYNTHETIC_KEY not in str(error)
    assert SYNTHETIC_KEY not in (error.body or "")
    assert SYNTHETIC_KEY not in "".join(traceback.format_exception(error))


@pytest.mark.asyncio
async def test_error_redaction_precedes_body_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = credential_config("bearer", monkeypatch)
    body = ("x" * 490) + SYNTHETIC_KEY + ("y" * 1470) + SYNTHETIC_KEY
    adapter = OpenAIChatAdapter(
        "mock",
        config,
        AuthStore(tmp_path / "auth.json"),
        transport=httpx.MockTransport(lambda _request: httpx.Response(401, text=body)),
    )
    try:
        with pytest.raises(ProviderError) as caught:
            await adapter.stream_chat(
                ChatRequest(model="model-a", messages=[]), session_id="ses", emit=noop
            )
    finally:
        await adapter.aclose()
    assert "audit-only" not in str(caught.value)
    assert "audit-only" not in (caught.value.body or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_nonretryable_errors_do_not_contact_fallback(tmp_path: Path, status: int) -> None:
    config = Config(
        providers={
            "primary": provider_config(base_url="https://primary.invalid/v1", max_retries=2),
            "backup": provider_config(base_url="https://backup.invalid/v1", max_retries=0),
        },
        roles=RolesConfig(main="primary/model-a", fallback=["backup/model-b"]),
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return (
            httpx.Response(status, json={"error": {"message": "primary failed"}})
            if (request.url.host == "primary.invalid")
            else success("backup")
        )

    registry = ProviderRegistry(
        config, AuthStore(tmp_path / "auth.json"), transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(ProviderError) as caught:
            await RoleRouter(config, registry).stream(
                "main", messages=[], tools=[], session_id="ses", emit=noop
            )
        assert caught.value.status == status
    finally:
        await registry.aclose()
    assert seen == ["primary.invalid"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_retryable_errors_can_contact_fallback(tmp_path: Path, status: int) -> None:
    config = Config(
        providers={
            "primary": provider_config(base_url="https://primary.invalid/v1", max_retries=0),
            "backup": provider_config(base_url="https://backup.invalid/v1", max_retries=0),
        },
        roles=RolesConfig(main="primary/model-a", fallback=["backup/model-b"]),
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return (
            httpx.Response(status, text="unavailable")
            if (request.url.host == "primary.invalid")
            else success("backup")
        )

    registry = ProviderRegistry(
        config, AuthStore(tmp_path / "auth.json"), transport=httpx.MockTransport(handler)
    )
    try:
        result = await RoleRouter(config, registry).stream(
            "main", messages=[], tools=[], session_id="ses", emit=noop
        )
    finally:
        await registry.aclose()
    assert result.text == "backup"
    assert seen == ["primary.invalid", "backup.invalid"]


def save_text_profile(model: str, *, text_mode: bool) -> None:
    save_profile(
        ModelProfile(
            provider="mock",
            model=model,
            api="openai_chat",
            supports_tools=not text_mode,
            quirks=ModelQuirks(text_tool_protocol=text_mode),
        )
    )


@pytest.mark.asyncio
async def test_text_profile_executes_fenced_tool_and_replays_text_history(
    config: Config, store: Store, tmp_path: Path
) -> None:
    save_text_profile("model-a", text_mode=True)
    (tmp_path / "sentinel.txt").write_text("TEXT_TOOL_SENTINEL", encoding="utf-8")
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        if len(payloads) == 1:
            return success('```json\n{"name":"read_file","arguments":{"path":"sentinel.txt"}}\n```')
        return success("Read the sentinel.")

    registry = ProviderRegistry(
        config, AuthStore(tmp_path / "auth.json"), transport=httpx.MockTransport(handler)
    )
    loop = AgentLoop(
        config=config,
        store=store,
        router=RoleRouter(config, registry),
        tools=ToolRegistry(default_tools()),
    )
    session = store.create_session(cwd=str(tmp_path), model="mock/model-a")
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(event_type: str, data: dict[str, Any]) -> None:
        events.append((event_type, data))

    try:
        result = await loop.run(session, "Read sentinel.txt.", emit=emit)
    finally:
        await loop.close_jobs()
        await registry.aclose()
    assert result.error is None
    assert len(payloads) == 2
    assert [kind for kind, _data in events].count("tool.start") == 1
    assert store.get_tool_calls(session.id)[0]["name"] == "read_file"
    for payload in payloads:
        assert "tools" not in payload
        assert "tool_choice" not in payload
        assert all("tool_calls" not in message for message in payload["messages"])
        assert all(message["role"] != "tool" for message in payload["messages"])
    assert "TEXT_TOOL_SENTINEL" in json.dumps(payloads[1]["messages"])
    instructions = payloads[0]["messages"][0]["content"]
    assert "read_file" in instructions
    assert "arguments" in instructions
    assert "```json" in instructions


@pytest.mark.asyncio
@pytest.mark.parametrize("models", [("text", "native", "text"), ("native", "text", "native")])
async def test_protocol_is_per_model_without_changing_other_request_settings(
    config: Config, tmp_path: Path, models: tuple[str, str, str]
) -> None:
    save_text_profile("text", text_mode=True)
    save_text_profile("native", text_mode=False)
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return success('```json\n{"name":"read_file","arguments":{"path":"a.txt"}}\n```')

    registry = ProviderRegistry(
        config, AuthStore(tmp_path / "auth.json"), transport=httpx.MockTransport(handler)
    )
    messages = [
        Message(role="system", content="Original system."),
        Message(role="user", content="hi"),
    ]
    try:
        adapter = registry.adapter("mock")
        for model in models:
            result = await RoleRouter(config, registry).stream(
                "main",
                model_override=f"mock/{model}",
                messages=messages,
                tools=[READ_TOOL],
                temperature=0.3,
                max_tokens=321,
                effort="high",
                session_id="ses",
                emit=noop,
            )
            assert bool(result.tool_calls) == (model == "text")
            assert registry.adapter("mock") is adapter
    finally:
        await registry.aclose()
    assert messages[0].content == "Original system."
    for model, payload in zip(models, payloads, strict=True):
        assert ("tools" in payload) == (model == "native")
        assert payload["model"] == model
        assert payload["temperature"] == 0.3
        assert payload["max_tokens"] == 321
        assert payload["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_unprofiled_models_keep_native_tools(config: Config, tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return success('```json\n{"name":"read_file","arguments":{"path":"a.txt"}}\n```')

    registry = ProviderRegistry(
        config, AuthStore(tmp_path / "auth.json"), transport=httpx.MockTransport(handler)
    )
    try:
        result = await RoleRouter(config, registry).stream(
            "main", messages=[], tools=[READ_TOOL], session_id="ses", emit=noop
        )
    finally:
        await registry.aclose()
    assert seen[0]["tools"] == [READ_TOOL.to_openai()]
    assert not result.tool_calls


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "-1", "garbage"])
def test_retry_after_rejects_invalid_delays(value: str) -> None:
    assert _retry_after(value) is None


@pytest.mark.asyncio
async def test_retry_emits_visible_progress_before_sleep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Config(
        providers={"mock": provider_config(max_retries=1)},
        roles=RolesConfig(main="mock/model-a"),
    )
    monkeypatch.setattr("siwah.providers.registry.random.uniform", lambda *_args: 0.0)
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, text="rate limited")
        return success()

    registry = ProviderRegistry(
        config, AuthStore(tmp_path / "auth.json"), transport=httpx.MockTransport(handler)
    )
    events: list[ProviderEvent] = []

    async def emit(event: ProviderEvent) -> None:
        events.append(event)

    try:
        result = await RoleRouter(config, registry).stream(
            "main", messages=[], tools=[], session_id="ses", emit=emit
        )
    finally:
        await registry.aclose()
    assert result.text == "ok"
    assert len(sleeps) == 1
    retries = [event.retry for event in events if event.type == "retry" and event.retry]
    assert len(retries) == 1
    retry = retries[0]
    assert retry is not None
    assert retry.attempt == 1
    assert retry.max_attempts == 2
    assert retry.delay_s == sleeps[0]
    assert retry.code
    assert retry.model == "model-a"


@pytest.mark.asyncio
async def test_timeout_retries_after_reasoning_and_resets_the_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Config(
        providers={"mock": provider_config(max_retries=1)},
        roles=RolesConfig(main="mock/model-a"),
    )
    registry = ProviderRegistry(config, AuthStore(tmp_path / "auth.json"))
    events: list[ProviderEvent] = []

    class ReasoningTimeoutAdapter:
        api = "openai_chat"
        provider_name = "mock"

        def __init__(self) -> None:
            self.config = config.providers["mock"]
            self.calls = 0

        async def stream_chat(self, request, *, session_id, emit):
            self.calls += 1
            await emit(ProviderEvent(type="reasoning.delta", text=f"thinking {self.calls} "))
            if self.calls == 1:
                raise ProviderError(
                    "Provider stream timed out (ReadTimeout)",
                    code="provider_timeout",
                    retryable=True,
                )
            await emit(ProviderEvent(type="text.delta", text="done"))
            return StreamOutcome(
                provider="mock", model="model-a", text="done", finish_reason="stop"
            )

        async def aclose(self):
            return None

    adapter = ReasoningTimeoutAdapter()
    registry._adapters["mock"] = adapter  # type: ignore[assignment]

    async def emit(event: ProviderEvent) -> None:
        events.append(event)

    async def sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("siwah.providers.registry.asyncio.sleep", sleep)
    try:
        result = await RoleRouter(config, registry).stream(
            "main", messages=[], tools=[], session_id="ses", emit=emit
        )
    finally:
        await registry.aclose()
    assert result.text == "done"
    assert adapter.calls == 2
    types = [event.type for event in events]
    assert "reset" in types
    assert "retry" in types
    assert types.index("reset") < types.index("retry")


@pytest.mark.asyncio
async def test_timeout_error_names_transport_budget_and_model(tmp_path: Path) -> None:
    config = Config(
        providers={"mock": provider_config(base_url="https://mock.invalid/v1", max_retries=0)},
        roles=RolesConfig(main="mock/model-a"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("", request=request)

    registry = ProviderRegistry(
        config, AuthStore(tmp_path / "auth.json"), transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(ProviderError) as caught:
            await RoleRouter(config, registry).stream(
                "main", messages=[], tools=[], session_id="ses", emit=noop
            )
    finally:
        await registry.aclose()
    message = str(caught.value)
    assert "ReadTimeout" in message
    assert "120s" in message
    assert "model-a" in message
    assert "mock.invalid" in message
    assert caught.value.code == "provider_timeout"
    assert caught.value.retryable is True


@pytest.mark.asyncio
@pytest.mark.parametrize("header", ["http-date", "NaN", "Infinity", "-5", "999999999999"])
async def test_retry_sleep_is_finite_bounded_and_honors_http_date(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, header: str
) -> None:
    now = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    import siwah.providers.openai_chat as openai_chat

    monkeypatch.setattr(openai_chat, "datetime", FixedDatetime, raising=False)
    monkeypatch.setattr("siwah.providers.registry.random.uniform", lambda *_args: 0.0)
    config.providers["mock"].max_retries = 1
    value = (
        format_datetime(now + timedelta(seconds=90), usegmt=True)
        if header == "http-date"
        else header
    )
    delays: list[float] = []
    calls = 0

    async def sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", sleep)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": value}, text="rate limited")
        return success()

    registry = ProviderRegistry(
        config, AuthStore(tmp_path / "auth.json"), transport=httpx.MockTransport(handler)
    )
    try:
        result = await RoleRouter(config, registry).stream(
            "main", messages=[], tools=[], session_id="ses", emit=noop
        )
    finally:
        await registry.aclose()
    assert result.text == "ok"
    assert calls == 2
    assert len(delays) == 1
    assert math.isfinite(delays[0])
    assert 0 <= delays[0] <= 300
    if header == "http-date":
        assert delays == [90.0]
    elif header in {"NaN", "Infinity", "-5"}:
        assert delays == [0.4]


def save_privacy_profile(provider: str, model: str, training: bool | None) -> None:
    save_profile(
        ModelProfile(
            provider=provider,
            model=model,
            api="openai_chat",
            privacy=PrivacyProfile(training=training),
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("training", [True, None, "missing", "malformed"])
async def test_private_routing_never_contacts_unverified_fallback(
    tmp_path: Path, training: bool | None | str
) -> None:
    config = Config(
        providers={"mock": provider_config(max_retries=0)},
        roles=RolesConfig(main="mock/private", fallback=["mock/public"]),
    )
    save_privacy_profile("mock", "private", False)
    if training == "malformed":
        path = profile_path("mock", "public")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not valid TOML", encoding="utf-8")
    elif training != "missing":
        assert not isinstance(training, str)
        save_privacy_profile("mock", "public", training)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen.append(model)
        return httpx.Response(429, text="limited") if model == "private" else success()

    registry = ProviderRegistry(
        config, AuthStore(tmp_path / "auth.json"), transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(ProviderError):
            await RoleRouter(config, registry).stream(
                "reflector",
                model_override="mock/private",
                messages=[Message(role="user", content="PRIVATE_TRAJECTORY")],
                tools=[],
                session_id="ses",
                emit=noop,
                require_private=True,
            )
    finally:
        await registry.aclose()
    assert seen == ["private"]


@pytest.mark.asyncio
@pytest.mark.parametrize("training", [True, None, "missing"])
async def test_private_routing_fails_closed_without_any_eligible_model(
    config: Config, tmp_path: Path, training: bool | None | str
) -> None:
    if training != "missing":
        assert not isinstance(training, str)
        save_privacy_profile("mock", "model-a", training)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return success()

    registry = ProviderRegistry(
        config, AuthStore(tmp_path / "auth.json"), transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(ProviderError) as caught:
            await RoleRouter(config, registry).stream(
                "main", messages=[], tools=[], session_id="ses", emit=noop, require_private=True
            )
        assert caught.value.code == "provider_privacy"
    finally:
        await registry.aclose()
    assert not seen


@pytest.mark.asyncio
async def test_private_routing_skips_public_primary_and_reports_actual_private_fallback(
    tmp_path: Path,
) -> None:
    config = Config(
        providers={"mock": provider_config(max_retries=0)},
        roles=RolesConfig(main="mock/public", fallback=["mock/private"]),
    )
    save_privacy_profile("mock", "public", True)
    save_privacy_profile("mock", "private", False)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["model"])
        return success()

    registry = ProviderRegistry(
        config, AuthStore(tmp_path / "auth.json"), transport=httpx.MockTransport(handler)
    )
    try:
        result = await RoleRouter(config, registry).stream(
            "main", messages=[], tools=[], session_id="ses", emit=noop, require_private=True
        )
    finally:
        await registry.aclose()
    assert seen == ["private"]
    assert result.provider == "mock"
    assert result.model == "private"


@pytest.mark.asyncio
async def test_private_routing_revalidates_privacy_before_retry(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.providers["mock"].max_retries = 1
    save_privacy_profile("mock", "model-a", False)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(429, text="limited")

    async def sleep(_delay: float) -> None:
        save_privacy_profile("mock", "model-a", True)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    registry = ProviderRegistry(
        config, AuthStore(tmp_path / "auth.json"), transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(ProviderError):
            await RoleRouter(config, registry).stream(
                "main", messages=[], tools=[], session_id="ses", emit=noop, require_private=True
            )
    finally:
        await registry.aclose()
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_normal_router_can_still_use_public_model(config: Config, tmp_path: Path) -> None:
    save_privacy_profile("mock", "model-a", True)
    registry = ProviderRegistry(
        config,
        AuthStore(tmp_path / "auth.json"),
        transport=httpx.MockTransport(lambda _r: success()),
    )
    try:
        result = await RoleRouter(config, registry).stream(
            "main", messages=[], tools=[], session_id="ses", emit=noop
        )
    finally:
        await registry.aclose()
    assert result.model == "model-a"


@pytest.mark.asyncio
async def test_partial_output_prevents_retry_and_fallback(tmp_path: Path) -> None:
    config = Config(
        providers={
            "primary": provider_config(base_url="https://primary.invalid/v1", max_retries=2),
            "backup": provider_config(base_url="https://backup.invalid/v1", max_retries=0),
        },
        roles=RolesConfig(main="primary/model-a", fallback=["backup/model-b"]),
    )
    seen: list[str] = []
    events: list[ProviderEvent] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        # Deliberately omit both terminal markers to simulate truncation.
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"PARTIAL"}}]}\n\n')

    async def emit(event: ProviderEvent) -> None:
        events.append(event)

    registry = ProviderRegistry(
        config, AuthStore(tmp_path / "auth.json"), transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(ProviderError) as caught:
            await RoleRouter(config, registry).stream(
                "main", messages=[], tools=[], session_id="ses", emit=emit
            )
        assert caught.value.code == "provider_bad_stream"
    finally:
        await registry.aclose()
    assert seen == ["primary.invalid"]
    assert [event.text for event in events] == ["PARTIAL"]


@pytest.mark.asyncio
async def test_text_protocol_converts_native_history_and_preserves_extras(
    config: Config, tmp_path: Path
) -> None:
    save_text_profile("text", text_mode=True)
    messages = [
        Message(role="user", content="Read a.txt."),
        Message(
            role="assistant",
            tool_calls=[ToolCall(id="old_native", name="read_file", arguments={"path": "a.txt"})],
        ),
        Message(role="tool", tool_call_id="old_native", content="READ_RESULT"),
    ]
    request = ChatRequest(
        model="text",
        messages=messages,
        tools=[READ_TOOL],
        extra={"seed": 123, "tools": [READ_TOOL.to_openai()], "tool_choice": "required"},
    )
    original = request.model_dump()
    seen: list[dict[str, Any]] = []

    def handler(wire_request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(wire_request.content))
        return success()

    registry = ProviderRegistry(
        config, AuthStore(tmp_path / "auth.json"), transport=httpx.MockTransport(handler)
    )
    try:
        await registry.adapter("mock").stream_chat(request, session_id="ses", emit=noop)
    finally:
        await registry.aclose()
    payload = seen[0]
    assert payload["seed"] == 123
    assert "tools" not in payload
    assert "tool_choice" not in payload
    assert payload["messages"][0]["role"] == "system"
    assert all(message["role"] != "tool" for message in payload["messages"])
    assert all("tool_calls" not in message for message in payload["messages"])
    assert "old_native" in payload["messages"][-2]["content"]
    assert '"path": "a.txt"' in payload["messages"][-2]["content"]
    assert "READ_RESULT" in payload["messages"][-1]["content"]
    assert request.model_dump() == original


@pytest.mark.asyncio
async def test_private_fallback_reports_actual_provider_and_model(tmp_path: Path) -> None:
    config = Config(
        providers={
            "primary": provider_config(base_url="https://primary.invalid/v1", max_retries=0),
            "backup": provider_config(base_url="https://backup.invalid/v1", max_retries=0),
        },
        roles=RolesConfig(main="primary/private-a", fallback=["backup/private-b"]),
    )
    save_privacy_profile("primary", "private-a", False)
    save_privacy_profile("backup", "private-b", False)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return (
            httpx.Response(429, text="limited")
            if request.url.host == "primary.invalid"
            else success("private backup")
        )

    registry = ProviderRegistry(
        config, AuthStore(tmp_path / "auth.json"), transport=httpx.MockTransport(handler)
    )
    try:
        result = await RoleRouter(config, registry).stream(
            "main", messages=[], tools=[], session_id="ses", emit=noop, require_private=True
        )
    finally:
        await registry.aclose()
    assert seen == ["primary.invalid", "backup.invalid"]
    assert result.provider == "backup"
    assert result.model == "private-b"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("", None), ("0", 0.0), ("1.5", 1.5), ("999999999", 300.0)],
)
def test_retry_after_numeric_and_empty_values(value: str | None, expected: float | None) -> None:
    assert _retry_after(value) == expected


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0, 999999999.0])
def test_provider_error_bounds_delays_from_other_adapters(value: float) -> None:
    error = ProviderError("limited", retryable=True, retry_after_s=value)
    if not math.isfinite(value) or value < 0:
        assert error.retry_after_s is None
    else:
        assert error.retry_after_s == 300.0
