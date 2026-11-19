"""In-memory event bus with replay for SSE clients."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import deque
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

from .schema import EventEnvelope


class EventBus:
    def __init__(self, session_id: str, retention: int = 2000) -> None:
        self.session_id = session_id
        self._events: deque[EventEnvelope] = deque(maxlen=retention)
        self._subscribers: set[asyncio.Queue[EventEnvelope]] = set()
        self._sequence = 0
        self._lock = asyncio.Lock()

    async def publish(
        self, event_type: str, data: dict[str, Any], *, turn_id: str | None = None
    ) -> EventEnvelope:
        async with self._lock:
            self._sequence += 1
            envelope = EventEnvelope(
                id=f"evt_{uuid.uuid4().hex}",
                seq=self._sequence,
                type=event_type,
                session_id=self.session_id,
                turn_id=turn_id,
                ts=datetime.now(UTC).isoformat(),
                data=data,
            )
            self._events.append(envelope)
            for queue in tuple(self._subscribers):
                if not queue.full():
                    queue.put_nowait(envelope)
        return envelope

    def history(self, after: int = 0) -> list[EventEnvelope]:
        return [event for event in self._events if event.seq > after]

    def last_seq(self) -> int:
        return self._sequence

    def replay_gap(self, after: int) -> bool:
        """True when a client cursor cannot be served without losing events.

        A cursor past the current sequence (for example after a daemon restart)
        or before the retained window must trigger client rehydration instead
        of silently withholding the next run.end event.
        """

        if after < 0 or after > self._sequence:
            return True
        if after == 0 or not self._events:
            return False
        return after < self._events[0].seq - 1

    async def subscribe(self, after: int = 0) -> AsyncGenerator[EventEnvelope, None]:
        # Unbounded on purpose: dropping events from a live subscriber silently
        # breaks the append-only stream contract. The generator is cancelled on
        # disconnect, so the queue lifetime is tied to the connection.
        queue: asyncio.Queue[EventEnvelope] = asyncio.Queue()
        # Register before replaying so an event published during replay cannot be lost.
        async with self._lock:
            self._subscribers.add(queue)
            for event in self.history(after):
                if not queue.full():
                    queue.put_nowait(event)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield _heartbeat(self.session_id, self._sequence)
                    continue
                yield event
        finally:
            self._subscribers.discard(queue)


def _heartbeat(session_id: str, seq: int) -> EventEnvelope:
    return EventEnvelope(
        id=f"evt_heartbeat_{seq}",
        seq=seq,
        type="heartbeat",
        session_id=session_id,
        ts=datetime.now(UTC).isoformat(),
        data={"state": "heartbeat"},
    )


def sse_frame(event: EventEnvelope) -> str:
    payload = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.seq}\nevent: {event.type}\ndata: {payload}\n\n"
