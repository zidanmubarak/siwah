"""Provider registry and role fallback routing."""

from __future__ import annotations

import asyncio
import random
from pathlib import Path

import httpx

from ..auth import AuthStore
from ..config import Config, ConfigError, ModelRef
from ..core.types import (
    ChatRequest,
    Message,
    ModelInfo,
    ProviderEvent,
    RetryNotice,
    StreamOutcome,
    ToolSpec,
)
from .base import EventSink, ProviderAdapter, ProviderError, bounded_retry_delay
from .openai_chat import OpenAIChatAdapter
from .profiles import load_profile

MAX_TIMEOUT_ATTEMPTS = 5


class ProviderRegistry:
    def __init__(
        self,
        config: Config,
        auth: AuthStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        record_dir: Path | None = None,
    ) -> None:
        self.config = config
        self.auth = auth
        self.transport = transport
        self.record_dir = record_dir
        self._adapters: dict[str, ProviderAdapter] = {}

    def adapter(self, provider: str) -> ProviderAdapter:
        if provider in self._adapters:
            return self._adapters[provider]
        provider_config = self.config.providers.get(provider)
        if provider_config is None:
            raise ConfigError(f"Unknown provider '{provider}'")
        if provider_config.api != "openai_chat":
            raise ConfigError(
                f"Provider '{provider}' uses '{provider_config.api}', which is not implemented "
                "yet. Use an openai_chat provider."
            )
        record_path = None
        if self.record_dir is not None:
            record_path = self.record_dir / f"{provider}__{len(self._adapters)}.json"
        adapter: ProviderAdapter = OpenAIChatAdapter(
            provider,
            provider_config,
            self.auth,
            transport=self.transport,
            record_path=record_path,
        )
        self._adapters[provider] = adapter
        return adapter

    async def list_models(self, provider: str) -> list[ModelInfo]:
        return await self.adapter(provider).list_models()

    async def aclose(self) -> None:
        for adapter in self._adapters.values():
            await adapter.aclose()
        self._adapters.clear()


class RoleRouter:
    def __init__(self, config: Config, registry: ProviderRegistry) -> None:
        self.config = config
        self.registry = registry

    def model_refs(self, role: str, override: str | None = None) -> list[ModelRef]:
        primary = self.config.model_for_role(role, override)
        refs = [primary]
        for ref in self.config.fallback_refs():
            if ref not in refs:
                refs.append(ref)
        return refs

    async def stream(
        self,
        role: str,
        *,
        messages: list[Message],
        tools: list[ToolSpec],
        session_id: str,
        emit: EventSink,
        model_override: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        effort: str | None = None,
        require_private: bool = False,
    ) -> StreamOutcome:
        refs = self.model_refs(role, model_override)
        last_error: ProviderError | None = None
        started = False
        for ref in refs:
            # Reject unknown/public models before constructing an adapter or
            # resolving credentials. A later private fallback can still serve.
            if require_private and not _is_private(ref):
                continue
            adapter = self.registry.adapter(ref.provider)
            max_attempts = max(1, adapter.config.max_retries + 1)  # type: ignore[attr-defined]
            timeout_attempts = 0
            attempt = 0
            privacy_blocked = False
            while True:
                # Profiles can change while a retry sleeps; recheck every
                # physical attempt, not only the requested logical model.
                if require_private and not _is_private(ref):
                    privacy_blocked = True
                    break

                async def wrapped(event) -> None:
                    nonlocal started
                    started = True
                    await emit(event)

                request = ChatRequest(
                    model=ref.model,
                    messages=messages,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    effort=effort,
                )
                try:
                    return await adapter.stream_chat(request, session_id=session_id, emit=wrapped)
                except ProviderError as exc:
                    last_error = exc
                    if not exc.retryable:
                        break
                    # Non-timeout failures after partial output stay terminal:
                    # replaying them could duplicate already-visible content.
                    # A timeout is recoverable because the partial preview can
                    # be reset before the next physical attempt.
                    if started and exc.code != "provider_timeout":
                        break
                    if exc.code == "provider_timeout":
                        timeout_attempts += 1
                        if timeout_attempts >= MAX_TIMEOUT_ATTEMPTS:
                            break
                    elif attempt >= max_attempts - 1:
                        break
                    delay = bounded_retry_delay(exc.retry_after_s)
                    if delay is None:
                        delay = min(8.0, (0.4 * (2 ** min(attempt, 5))) + random.uniform(0.0, 0.25))
                    if started:
                        # Partial text/reasoning is not persisted yet. Clear the
                        # streamed preview before retrying so output is not duplicated.
                        await emit(ProviderEvent(type="reset"))
                        started = False
                    # Tell the client before sleeping so a slow provider does not
                    # look like a frozen run. Emit directly (not through
                    # ``wrapped``) so a retry notice never marks partial output
                    # as already delivered.
                    await emit(
                        ProviderEvent(
                            type="retry",
                            text=str(exc),
                            retry=RetryNotice(
                                provider=ref.provider,
                                model=ref.model,
                                attempt=attempt + 1,
                                max_attempts=max(max_attempts, MAX_TIMEOUT_ATTEMPTS)
                                if exc.code == "provider_timeout"
                                else max_attempts,
                                delay_s=delay,
                                code=exc.code or "provider_error",
                                message=str(exc),
                            ),
                        )
                    )
                    await asyncio.sleep(delay)
                    attempt += 1
            if privacy_blocked:
                continue
            if last_error is not None and not last_error.retryable:
                break
            if started and last_error is not None and last_error.code != "provider_timeout":
                break
            if started:
                # Switching providers after a timeout would duplicate the
                # preview; reset it before trying the next fallback.
                await emit(ProviderEvent(type="reset"))
                started = False
        if last_error is not None:
            raise last_error
        if require_private:
            raise ProviderError(
                "No model with explicitly disabled training could serve the request",
                code="provider_privacy",
            )
        raise ProviderError("No provider could serve the request", code="no_provider")


def _is_private(ref: ModelRef) -> bool:
    try:
        profile = load_profile(ref.provider, ref.model)
    except (OSError, ValueError):
        return False
    return (
        profile.provider == ref.provider
        and profile.model == ref.model
        and profile.privacy.training is False
    )


__all__ = ["ProviderRegistry", "RoleRouter"]
