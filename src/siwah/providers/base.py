"""Provider adapter contract and errors."""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from ..core.types import ChatRequest, ModelInfo, ProviderEvent, StreamOutcome

MAX_RETRY_DELAY_S = 300.0


def bounded_retry_delay(value: float | None) -> float | None:
    """Reject invalid delays and cap upstream waits at five minutes."""
    if value is None or not math.isfinite(value) or value < 0:
        return None
    return min(value, MAX_RETRY_DELAY_S)


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_error",
        retryable: bool = False,
        status: int | None = None,
        retry_after_s: float | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status = status
        self.retry_after_s = bounded_retry_delay(retry_after_s)
        self.body = body


EventSink = Callable[[ProviderEvent], Awaitable[None]]


class ProviderAdapter(ABC):
    provider_name: str
    api: str

    @abstractmethod
    async def list_models(self) -> list[ModelInfo]: ...

    @abstractmethod
    async def stream_chat(
        self,
        request: ChatRequest,
        *,
        session_id: str,
        emit: EventSink,
    ) -> StreamOutcome: ...

    async def aclose(self) -> None:
        return None
