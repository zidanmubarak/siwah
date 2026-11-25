"""OpenAI-compatible Chat Completions adapter."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

from ..auth import AuthStore
from ..config import EFFORT_VALUES, ProviderConfig
from ..core.types import (
    ChatRequest,
    ModelInfo,
    ProviderEvent,
    StreamOutcome,
    ToolCall,
    Usage,
)
from ..version import __version__
from .base import EventSink, ProviderAdapter, ProviderError, bounded_retry_delay
from .fixtures import append_exchange
from .profiles import load_profile
from .repair import (
    ToolArgumentError,
    extract_text_tool_calls,
    has_trailing_object,
    parse_tool_arguments,
)


class OpenAIChatAdapter(ProviderAdapter):
    api = "openai_chat"

    def __init__(
        self,
        provider_name: str,
        config: ProviderConfig,
        auth: AuthStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        record_path: Path | None = None,
        allow_text_tool_calls: bool = False,
    ) -> None:
        self.provider_name = provider_name
        self.config = config
        self._auth = auth
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(config.timeout_s, connect=config.connect_timeout_s),
            transport=transport,
        )
        self._record_path = record_path
        self._allow_text_tool_calls = allow_text_tool_calls
        self._models: dict[str, ModelInfo] = {}

    def cached_model(self, model: str) -> ModelInfo | None:
        return self._models.get(model)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self, session_id: str) -> dict[str, str]:
        headers = {
            key: value.format(version=__version__, session_id=session_id)
            for key, value in self.config.headers.items()
        }
        headers.setdefault("User-Agent", f"siwah/{__version__}")
        headers.setdefault("Accept", "text/event-stream")
        headers.setdefault("Content-Type", "application/json")
        if self.config.auth.scheme == "bearer":
            headers["Authorization"] = (
                f"Bearer {self._auth.require_key(self.provider_name, self.config.auth)}"
            )
        return headers

    async def list_models(self) -> list[ModelInfo]:
        url = self.config.models_url
        if not url:
            raise ProviderError(
                f"Provider '{self.provider_name}' has no models_url configured",
                code="models_unavailable",
            )
        headers = self._headers("models")
        secrets = _credential_values(headers)
        try:
            response = await self._client.get(url, headers=headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _http_error(
                exc.response.status_code, exc.response.text, secrets=secrets
            ) from None
        except httpx.HTTPError as exc:
            raise ProviderError(
                _redact(f"Models request failed: {exc}", secrets),
                code="models_unavailable",
                retryable=True,
            ) from None
        try:
            payload = response.json()
        except ValueError:
            raise ProviderError(
                "Models response was not valid JSON", code="models_bad_response"
            ) from None
        entries = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            raise ProviderError(
                "Models response did not contain a data list", code="models_bad_response"
            )
        models: list[ModelInfo] = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                continue
            endpoints = entry.get("supported_endpoints")
            if isinstance(endpoints, list) and not any(
                isinstance(value, str) and value.rstrip("/").endswith("/chat/completions")
                for value in endpoints
            ):
                continue
            models.append(_model_info(entry))
        self._models = {model.id: model for model in models}
        return models

    async def stream_chat(
        self,
        request: ChatRequest,
        *,
        session_id: str,
        emit: EventSink,
    ) -> StreamOutcome:
        text_tool_protocol = False
        try:
            profile = load_profile(self.provider_name, request.model)
        except (OSError, ValueError):
            pass
        else:
            text_tool_protocol = (
                profile.provider == self.provider_name
                and profile.model == request.model
                and profile.uses_text_tool_protocol
            )
        model = self.cached_model(request.model)
        if (
            request.effort
            and model
            and (
                model.supports_reasoning is False
                or (model.efforts and request.effort not in model.efforts)
            )
        ):
            request = request.model_copy(update={"effort": None})
        payload = _payload(
            request,
            self.config.stream_usage,
            text_tool_protocol=text_tool_protocol,
            reasoning_format=self.config.reasoning_format,
            replay_reasoning=self.config.replay_reasoning,
        )
        headers = self._headers(session_id)
        secrets = _credential_values(headers)
        url = f"{self.config.base_url}/chat/completions"
        raw_lines: list[str] = []
        status = 0
        response_headers: dict[str, str] = {}
        finish_reason: str | None = None
        usage_raw: dict[str, Any] | None = None
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        partials: dict[int, dict[str, str]] = {}
        slot_ids: dict[str, int] = {}
        saw_terminal = False

        try:
            async with self._client.stream("POST", url, headers=headers, json=payload) as response:
                status = response.status_code
                response_headers = dict(response.headers)
                if status >= 400:
                    body = (await response.aread()).decode("utf-8", errors="replace")
                    raise _http_error(
                        status, body, response.headers.get("retry-after"), secrets=secrets
                    )
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    raw_lines.append(line)
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        saw_terminal = True
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise ProviderError(
                            f"Malformed stream event: {_redact(data, secrets)[:160]}",
                            code="provider_bad_stream",
                            retryable=True,
                        ) from exc
                    if isinstance(event, dict) and event.get("error"):
                        raise _stream_error(event["error"], secrets=secrets)
                    for choice in event.get("choices", []) if isinstance(event, dict) else []:
                        delta = choice.get("delta") or {}
                        content = _text_of(delta.get("content"))
                        if content:
                            text_parts.append(content)
                            await emit(ProviderEvent(type="text.delta", text=content))
                        reasoning = _text_of(
                            delta.get("reasoning_content") or delta.get("reasoning")
                        )
                        if reasoning:
                            reasoning_parts.append(reasoning)
                            await emit(ProviderEvent(type="reasoning.delta", text=reasoning))
                        for position, call in enumerate(delta.get("tool_calls") or []):
                            if not isinstance(call, dict):
                                continue
                            index = _resolve_tool_index(call, position, partials, slot_ids)
                            slot = partials.setdefault(
                                index, {"id": "", "name": "", "arguments": ""}
                            )
                            if call.get("id"):
                                slot["id"] = str(call["id"])
                            function = call.get("function") or {}
                            if function.get("name"):
                                name = str(function["name"])
                                if not slot["name"]:
                                    slot["name"] = name
                                elif not slot["name"].endswith(name):
                                    slot["name"] += name
                            if function.get("arguments") is not None:
                                fragment = str(function["arguments"])
                                slot["arguments"] += fragment
                                await emit(
                                    ProviderEvent(
                                        type="tool_call.delta",
                                        index=index,
                                        tool_call_id=slot["id"] or None,
                                        tool_name=slot["name"] or None,
                                        arguments_delta=fragment,
                                    )
                                )
                        if choice.get("finish_reason"):
                            finish_reason = str(choice["finish_reason"])
                            saw_terminal = True
                    if isinstance(event, dict) and event.get("usage"):
                        usage_raw = event["usage"]
        except httpx.TimeoutException as exc:
            kind = type(exc).__name__
            detail = str(exc).strip() or "transport did not provide a detail"
            if isinstance(exc, httpx.ConnectTimeout):
                action = "connecting to"
            elif isinstance(exc, httpx.ReadTimeout):
                action = "waiting for stream data from"
            else:
                action = "requesting"
            host = httpx.URL(self.config.base_url).host
            raise ProviderError(
                _redact(
                    f"Provider stream timed out ({kind}) after "
                    f"{self.config.timeout_s:g}s {action} {request.model}"
                    f" at {host}: {detail}. Retry, switch model, or raise "
                    "the provider timeout_s.",
                    secrets,
                ),
                code="provider_timeout",
                retryable=True,
            ) from None
        except httpx.HTTPError as exc:
            raise ProviderError(
                _redact(f"Provider request failed: {exc}", secrets),
                code="provider_transport",
                retryable=True,
            ) from None

        if not saw_terminal:
            raise ProviderError(
                "Provider stream ended without finish_reason or [DONE]",
                code="provider_bad_stream",
                retryable=True,
            )

        tool_calls = _assemble_tool_calls(partials)
        if not tool_calls and (text_tool_protocol or self._allow_text_tool_calls):
            allowed = {tool.name for tool in request.tools}
            for index, call in enumerate(extract_text_tool_calls("".join(text_parts), allowed)):
                tool_calls.append(
                    ToolCall(
                        id=f"text_call_{index}",
                        name=call["name"],
                        arguments=call["arguments"],
                    )
                )

        usage = normalize_usage(usage_raw, self.config.cost_field)
        outcome = StreamOutcome(
            provider=self.provider_name,
            model=request.model,
            text="".join(text_parts),
            reasoning="".join(reasoning_parts),
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=finish_reason,
        )
        await emit(ProviderEvent(type="usage", usage=usage))
        await emit(ProviderEvent(type="finish", finish_reason=finish_reason))
        if self._record_path is not None:
            append_exchange(
                self._record_path,
                url=url,
                request_body=payload,
                status=status,
                response_headers=response_headers,
                response_body="\n".join(raw_lines),
            )
        return outcome


def _payload(
    request: ChatRequest,
    stream_usage: bool,
    *,
    text_tool_protocol: bool = False,
    reasoning_format: str = "flat",
    replay_reasoning: bool = False,
) -> dict[str, Any]:
    messages = [message.to_openai() for message in request.messages]
    if replay_reasoning and request.tools:
        for message, serialized in zip(request.messages, messages, strict=True):
            if message.role == "assistant" and message.reasoning:
                serialized["reasoning_content"] = message.reasoning
    payload: dict[str, Any] = {
        "model": request.model,
        "messages": messages,
        "stream": True,
    }
    if stream_usage:
        payload["stream_options"] = {"include_usage": True}
    if request.tools:
        payload["tools"] = [tool.to_openai() for tool in request.tools]
        payload["tool_choice"] = "auto"
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.max_tokens is not None:
        payload["max_tokens"] = request.max_tokens
    if request.effort in EFFORT_VALUES:
        if reasoning_format == "openrouter":
            payload["reasoning"] = {"effort": request.effort}
        else:
            payload["reasoning_effort"] = request.effort
    payload.update(request.extra)
    if text_tool_protocol:
        # Apply after extras too: this endpoint does not support native tools.
        for key in ("tools", "tool_choice", "parallel_tool_calls", "functions", "function_call"):
            payload.pop(key, None)
        payload["messages"] = _text_tool_messages(request)
    return payload


def _text_tool_messages(request: ChatRequest) -> list[dict[str, Any]]:
    """Describe and replay tools as plain messages without mutating history."""
    messages: list[dict[str, Any]] = []
    for message in request.messages:
        item = message.to_openai()
        item.pop("tool_calls", None)
        item.pop("tool_call_id", None)
        if message.role == "tool":
            item["role"] = "user"
            item["content"] = f"Tool result ({message.tool_call_id}):\n{message.content or ''}"
        elif message.tool_calls and not extract_text_tool_calls(message.content or ""):
            calls = [
                "```json\n"
                + json.dumps(
                    {"id": call.id, "name": call.name, "arguments": call.arguments},
                    ensure_ascii=False,
                )
                + "\n```"
                for call in message.tool_calls
            ]
            item["content"] = "\n".join([message.content or "", *calls]).strip()
        messages.append(item)
    if request.tools:
        instructions = (
            "## Text tool protocol\n"
            "Native tool calling is unavailable. To call a tool, reply with a fenced JSON "
            'object:\n```json\n{"name":"<tool name>","arguments":{...}}\n```\n'
            "Use only the tools listed below and wait for their results before continuing. "
            "Tool results arrive as user messages labeled 'Tool result'. "
            "Otherwise answer normally without a tool-call object.\n"
            "Available tools:\n"
            + json.dumps(
                [tool.to_openai()["function"] for tool in request.tools], ensure_ascii=False
            )
        )
        if messages and messages[0]["role"] == "system":
            messages[0]["content"] = f"{messages[0]['content']}\n\n{instructions}"
        else:
            messages.insert(0, {"role": "system", "content": instructions})
    return messages


def _resolve_tool_index(
    call: dict[str, Any],
    position: int,
    partials: dict[int, dict[str, str]],
    slot_ids: dict[str, int],
) -> int:
    """Resolve a tool-call slot, preferring durable ids over positional index."""

    raw_id = call.get("id")
    call_id = str(raw_id) if raw_id else ""
    if call_id and call_id in slot_ids:
        return slot_ids[call_id]
    raw_index = call.get("index")
    index: int | None = None
    if raw_index is not None:
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            index = None
    if index is None:
        index = max(partials, default=-1) + 1 if call_id else position
    elif call_id and partials.get(index, {}).get("id") not in (None, "", call_id):
        index = max(partials, default=-1) + 1
    if call_id:
        slot_ids[call_id] = index
    return index


def _assemble_tool_calls(partials: dict[int, dict[str, str]]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for index in sorted(partials):
        slot = partials[index]
        if not slot["name"]:
            continue
        raw = slot["arguments"]
        valid = True
        try:
            arguments = parse_tool_arguments(raw)
        except ToolArgumentError:
            arguments = {}
            valid = False
        if valid and has_trailing_object(raw):
            valid = False
        calls.append(
            ToolCall(
                id=slot["id"] or f"call_{index}",
                name=slot["name"],
                arguments=arguments,
                raw_arguments=raw or None,
                arguments_valid=valid,
            )
        )
    return calls


def normalize_usage(raw: dict[str, Any] | None, cost_field: str) -> Usage:
    if not raw:
        return Usage()
    opencode_raw = raw.get("opencode")
    opencode: dict[str, Any] = opencode_raw if isinstance(opencode_raw, dict) else {}
    tokens_raw = opencode.get("tokens")
    tokens: dict[str, Any] = tokens_raw if isinstance(tokens_raw, dict) else {}
    prompt_raw = raw.get("prompt_tokens_details")
    if prompt_raw is None:
        prompt_raw = raw.get("input_tokens_details")
    prompt_details: dict[str, Any] = prompt_raw if isinstance(prompt_raw, dict) else {}
    completion_raw = raw.get("completion_tokens_details")
    if completion_raw is None:
        completion_raw = raw.get("output_tokens_details")
    completion_details: dict[str, Any] = completion_raw if isinstance(completion_raw, dict) else {}
    allowance = _reported_amount(_get_path(raw, cost_field))
    cost = _reported_amount(raw.get("cost"))
    return Usage(
        input_tokens=_as_int(
            raw.get("prompt_tokens", raw.get("input_tokens", tokens.get("input")))
        ),
        output_tokens=_as_int(
            raw.get("completion_tokens", raw.get("output_tokens", tokens.get("output")))
        ),
        reasoning_tokens=_as_int(
            completion_details.get("reasoning_tokens", tokens.get("reasoning"))
        ),
        cache_read_tokens=_as_int(
            prompt_details.get(
                "cached_tokens", raw.get("prompt_cache_hit_tokens", tokens.get("cache_read"))
            )
        ),
        cache_write_tokens=_as_int(tokens.get("cache_write_5m"))
        + _as_int(tokens.get("cache_write_1h")),
        allowance=allowance or 0.0,
        cost=cost or 0.0,
        allowance_reported=allowance is not None,
        cost_reported=cost is not None,
        list_cost=_as_float(opencode.get("list_cost")),
        billing_source=opencode.get("billing_source"),
        raw=raw,
    )


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _as_float(value: Any) -> float:
    return _reported_amount(value) or 0.0


def _reported_amount(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _get_path(value: dict[str, Any], dotted: str) -> Any:
    parts = [part for part in dotted.split(".") if part]
    if parts and parts[0] == "usage":
        parts = parts[1:]
    current: Any = value
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _text_of(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_text_of(item) for item in value)
    if isinstance(value, dict):
        return _text_of(value.get("text") or value.get("content"))
    return str(value)


def _model_info(entry: dict[str, Any]) -> ModelInfo:
    capabilities_raw = entry.get("capabilities")
    capabilities: dict[str, Any] = capabilities_raw if isinstance(capabilities_raw, dict) else {}
    limit_raw = entry.get("limit")
    limit: dict[str, Any] = limit_raw if isinstance(limit_raw, dict) else {}
    api_raw = entry.get("api")
    api: dict[str, Any] = api_raw if isinstance(api_raw, dict) else {}
    native_api = None
    reasoning_field = None
    for key, value in api.items():
        if isinstance(value, dict):
            native_api = _api_name(key)
            compatibility = value.get("compatibility")
            if isinstance(compatibility, dict):
                reasoning_field = compatibility.get("reasoning_field")
            break
    tools_raw = capabilities.get("tools")
    tools: dict[str, Any] = tools_raw if isinstance(tools_raw, dict) else {}
    reasoning_raw = capabilities.get("reasoning")
    reasoning: dict[str, Any] = reasoning_raw if isinstance(reasoning_raw, dict) else {}
    native_effort = entry.get("effort")
    efforts_raw = (
        native_effort.get("supported_levels")
        if isinstance(native_effort, dict)
        else reasoning.get("effort")
    )
    efforts = (
        [value for value in efforts_raw if value in EFFORT_VALUES]
        if isinstance(efforts_raw, list)
        else []
    )
    pricing_raw = entry.get("cost", entry.get("pricing"))
    pricing: dict[str, Any] = pricing_raw if isinstance(pricing_raw, dict) else {}
    privacy_raw = entry.get("privacy")
    privacy: dict[str, Any] = privacy_raw if isinstance(privacy_raw, dict) else {}
    top_provider = entry.get("top_provider")
    max_completion = (
        top_provider.get("max_completion_tokens") if isinstance(top_provider, dict) else None
    )
    supported_parameters = entry.get("supported_parameters")
    supports_tools = tools.get("supported")
    supports_reasoning = reasoning.get("supported")
    if efforts and not isinstance(supports_reasoning, bool):
        supports_reasoning = True
    if isinstance(supported_parameters, list):
        if not isinstance(supports_tools, bool):
            supports_tools = "tools" in supported_parameters
        if not isinstance(supports_reasoning, bool):
            supports_reasoning = any(
                field in supported_parameters for field in ("reasoning", "include_reasoning")
            )
    return ModelInfo(
        id=entry["id"],
        name=entry.get("name"),
        context_window=_int_or_none(
            limit.get("context") or entry.get("context_window") or entry.get("context_length")
        ),
        max_output=_int_or_none(
            limit.get("output") or entry.get("max_output_tokens") or max_completion
        ),
        supports_tools=supports_tools if isinstance(supports_tools, bool) else None,
        supports_reasoning=supports_reasoning if isinstance(supports_reasoning, bool) else None,
        efforts=efforts,
        reasoning_field=reasoning_field,
        native_api=native_api,
        pricing=pricing,
        privacy=privacy,
        raw=entry,
    )


def _api_name(value: str) -> str:
    return {
        "chat-completions": "openai_chat",
        "messages": "anthropic_messages",
        "responses": "openai_responses",
        "generate-content": "gemini",
    }.get(value, value)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _http_error(
    status: int,
    body: str,
    retry_after: str | None = None,
    *,
    secrets: tuple[str, ...] = (),
) -> ProviderError:
    message = _redact(body, secrets)[:500]
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            error = parsed.get("error")
            if isinstance(error, dict) and error.get("message"):
                message = str(error["message"])
            elif parsed.get("message"):
                message = str(parsed["message"])
    except json.JSONDecodeError:
        pass
    code = "provider_error"
    retryable = status == 429 or status >= 500
    if status in {401, 403}:
        code = "provider_auth"
    elif status == 429:
        code = "provider_rate_limited"
    elif status >= 500:
        code = "provider_unavailable"
    elif status == 400:
        code = "provider_bad_request"
    return ProviderError(
        _redact(message, secrets),
        code=code,
        retryable=retryable,
        status=status,
        retry_after_s=_retry_after(retry_after),
        body=_redact(body, secrets)[:2000],
    )


def _stream_error(error: Any, *, secrets: tuple[str, ...] = ()) -> ProviderError:
    message = str(error.get("message") or error) if isinstance(error, dict) else str(error)
    return ProviderError(_redact(message, secrets), code="provider_stream_error", retryable=False)


def _credential_values(headers: dict[str, str]) -> tuple[str, ...]:
    # Custom gateways can use arbitrarily named credential headers. Treat
    # non-public header values as sensitive rather than guessing key names.
    public_headers = {"user-agent", "accept", "content-type", "x-opencode-session"}
    values: set[str] = set()
    for name, value in headers.items():
        if name.lower() in public_headers or not value:
            continue
        values.add(value)
        if name.lower() in {"authorization", "proxy-authorization"}:
            parts = value.split(maxsplit=1)
            if len(parts) == 2:
                values.add(parts[1])
    # JSON wire bodies may escape characters even when parsed messages do not.
    values.update(json.dumps(value)[1:-1] for value in tuple(values))
    return tuple(sorted(values, key=len, reverse=True))


def _redact(value: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        value = value.replace(secret, "[REDACTED]")
    return value


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            seconds = max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None
    return bounded_retry_delay(seconds)


__all__ = ["OpenAIChatAdapter", "normalize_usage"]
