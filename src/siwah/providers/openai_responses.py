"""OpenAI Responses API adapter for the official ChatGPT plan flow."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from ..auth import AuthError
from ..config import EFFORT_VALUES, ProviderConfig
from ..core.types import ChatRequest, ModelInfo, ProviderEvent, StreamOutcome
from ..version import __version__
from .base import EventSink, ProviderAdapter, ProviderError
from .chatgpt_auth import CHATGPT_RESOURCE
from .openai_chat import (
    _assemble_tool_calls,
    _http_error,
    _model_info,
    _stream_error,
    normalize_usage,
)

BASE_URL = CHATGPT_RESOURCE
MODELS_URL = f"{BASE_URL}/models"
NAMESPACE = "siwah"


def supports_chatgpt_login(config: ProviderConfig) -> bool:
    return (
        config.api == "openai_responses"
        and config.auth.scheme == "bearer"
        and config.auth.browser == "chatgpt"
        and config.base_url == BASE_URL
        and config.models_url in {None, MODELS_URL}
    )


class OpenAIResponsesAdapter(ProviderAdapter):
    api = "openai_responses"

    def __init__(
        self,
        provider_name: str,
        config: ProviderConfig,
        access_token: Callable[[], Awaitable[str]],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.provider_name = provider_name
        self.config = config
        self._access_token = access_token
        self._models: dict[str, ModelInfo] = {}
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(config.timeout_s, connect=config.connect_timeout_s),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _headers(self) -> tuple[str, dict[str, str]]:
        try:
            token = await self._access_token()
        except AuthError as exc:
            raise ProviderError(str(exc), code="provider_auth") from None
        return token, {
            "Authorization": f"Bearer {token}",
            "User-Agent": f"siwah/{__version__}",
        }

    def cached_model(self, model: str) -> ModelInfo | None:
        return self._models.get(model)

    def clear_catalog(self) -> None:
        self._models.clear()

    async def list_models(self) -> list[ModelInfo]:
        token, headers = await self._headers()
        try:
            response = await self._client.get(MODELS_URL, headers=headers)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise _http_error(
                exc.response.status_code, exc.response.text, secrets=(token,)
            ) from None
        except (httpx.HTTPError, ValueError):
            raise ProviderError(
                "ChatGPT model catalog unavailable", code="models_unavailable"
            ) from None
        entries = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            raise ProviderError("ChatGPT model catalog is missing", code="models_bad_response")
        models = [
            _model_info(
                {
                    **item,
                    "id": item["slug"],
                    "name": item.get("display_name"),
                    "limit": {
                        "context": item.get("context_window"),
                        "output": item.get("max_output_tokens"),
                    },
                    "capabilities": {
                        "reasoning": {
                            "effort": [
                                value.get("effort") if isinstance(value, dict) else value
                                for value in item.get("supported_reasoning_levels") or []
                            ],
                        },
                    },
                }
            )
            for item in entries
            if isinstance(item, dict)
            and item.get("visibility") == "list"
            and isinstance(item.get("slug"), str)
        ]
        self._models = {model.id: model for model in models}
        return models

    async def stream_chat(
        self, request: ChatRequest, *, session_id: str, emit: EventSink
    ) -> StreamOutcome:
        token, headers = await self._headers()
        model = self.cached_model(request.model)
        if model and request.effort not in model.efforts:
            request = request.model_copy(update={"effort": None})
        text: list[str] = []
        reasoning: list[str] = []
        partials: dict[int, dict[str, str]] = {}
        completed = None
        try:
            async with self._client.stream(
                "POST",
                f"{BASE_URL}/responses",
                headers={**headers, "Accept": "text/event-stream"},
                json=_payload(request),
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", errors="replace")
                    try:
                        payload = json.loads(body)
                    except ValueError:
                        payload = None
                    error = _plan_error(payload.get("error")) if isinstance(payload, dict) else None
                    if error:
                        raise error
                    raise _http_error(
                        response.status_code,
                        body,
                        response.headers.get("retry-after"),
                        secrets=(token,),
                    )
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    event = json.loads(chunk)
                    if not isinstance(event, dict):
                        continue
                    kind = event.get("type")
                    delta = event.get("delta")
                    if kind == "response.output_text.delta" and isinstance(delta, str):
                        text.append(delta)
                        await emit(ProviderEvent(type="text.delta", text=delta))
                    elif kind == "response.reasoning_summary_text.delta" and isinstance(delta, str):
                        reasoning.append(delta)
                        await emit(ProviderEvent(type="reasoning.delta", text=delta))
                    elif kind == "response.function_call_arguments.delta" and isinstance(
                        delta, str
                    ):
                        await emit(
                            ProviderEvent(
                                type="tool_call.delta",
                                index=event.get("output_index"),
                                arguments_delta=delta,
                            )
                        )
                    elif kind == "response.output_item.done":
                        _add_call(partials, event.get("item"), event.get("output_index"))
                    elif kind == "response.completed":
                        completed = event.get("response")
                        break
                    elif kind == "response.incomplete":
                        raise ProviderError(
                            "ChatGPT response was incomplete", code="provider_incomplete"
                        )
                    elif kind in {"response.failed", "error"}:
                        response_item = event.get("response")
                        detail = (
                            response_item.get("error") if isinstance(response_item, dict) else None
                        ) or event
                        raise _plan_error(detail) or _stream_error(detail, secrets=(token,))
        except httpx.TimeoutException:
            raise ProviderError(
                "ChatGPT stream timed out", code="provider_timeout", retryable=True
            ) from None
        except (httpx.HTTPError, ValueError):
            raise ProviderError(
                "ChatGPT stream was interrupted", code="provider_bad_stream", retryable=True
            ) from None
        if not isinstance(completed, dict):
            raise ProviderError(
                "ChatGPT stream ended before completing", code="provider_bad_stream", retryable=True
            )
        for index, item in enumerate(completed.get("output") or []):
            _add_call(partials, item, index)
        usage = normalize_usage(completed.get("usage"), self.config.cost_field)
        calls = _assemble_tool_calls(partials)
        reason = "tool_calls" if calls else "stop"
        await emit(ProviderEvent(type="usage", usage=usage))
        await emit(ProviderEvent(type="finish", finish_reason=reason))
        return StreamOutcome(
            provider=self.provider_name,
            model=request.model,
            text="".join(text),
            reasoning="".join(reasoning),
            tool_calls=calls,
            usage=usage,
            finish_reason=reason,
        )


def _plan_error(detail: Any) -> ProviderError | None:
    if not isinstance(detail, dict):
        return None
    code = detail.get("code")
    if code == "subscription_sharing_usage_limit_exceeded":
        return ProviderError(
            "ChatGPT plan usage limit reached. Manage usage at https://chatgpt.com/settings/usage.",
            code=code,
            status=429,
        )
    if (
        code == "subscription_sharing_usage_unavailable"
        or code == "subscription_sharing_user_unavailable"
    ):
        return ProviderError(
            "ChatGPT plan usage is temporarily unavailable. Try again shortly.",
            code=code,
            status=503,
            retryable=True,
        )
    if code == "subscription_sharing_user_not_eligible":
        return ProviderError(
            "ChatGPT plan usage is unavailable for this account or workspace.",
            code=code,
            status=403,
        )
    return None


def _add_call(partials: dict[int, dict[str, str]], item: Any, index: Any) -> None:
    if (
        not isinstance(index, int)
        or not isinstance(item, dict)
        or item.get("type") != "function_call"
    ):
        return
    namespace, name = item.get("namespace"), item.get("name")
    call_id, arguments = item.get("call_id"), item.get("arguments")
    if (
        namespace not in (None, NAMESPACE)
        or not isinstance(name, str)
        or not isinstance(call_id, str)
        or not isinstance(arguments, str)
    ):
        return
    partials[index] = {"id": call_id, "name": name, "arguments": arguments}


def _payload(request: ChatRequest) -> dict[str, Any]:
    input_items: list[dict[str, Any]] = []
    for message in request.messages:
        if message.role == "tool":
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": message.content or "",
                }
            )
            continue
        if message.content or message.attachments:
            role = "developer" if message.role == "system" else message.role
            content: Any = message.content or ""
            if role == "user" and message.attachments:
                content = [{"type": "input_text", "text": content}] if content else []
                content.extend(
                    {"type": "input_image", "image_url": image.data_url(), "detail": "auto"}
                    for image in message.attachments
                    if image.mime.startswith("image/")
                )
            input_items.append({"role": role, "content": content})
        input_items.extend(
            {
                "type": "function_call",
                "call_id": call.id,
                "name": call.name,
                "namespace": NAMESPACE,
                "arguments": call.raw_arguments
                if call.raw_arguments is not None
                else json.dumps(call.arguments),
            }
            for call in message.tool_calls
        )
    payload: dict[str, Any] = {
        "model": request.model,
        "input": input_items,
        "store": False,
        "stream": True,
    }
    if request.tools:
        payload["tools"] = [
            {
                "type": "namespace",
                "name": NAMESPACE,
                "description": "Tools available in the local Siwah workspace.",
                "tools": [
                    {"type": "function", "strict": False, **tool.to_openai()["function"]}
                    for tool in request.tools
                ],
            }
        ]
    if request.effort in EFFORT_VALUES:
        payload["reasoning"] = {"effort": request.effort}
    return payload
