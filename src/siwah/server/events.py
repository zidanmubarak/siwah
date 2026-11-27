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

_TOOL_ACTIVITY_HISTORY = 128


def _is_terminal_activity(entry: dict[str, Any]) -> bool:
    data = entry.get("data")
    if not isinstance(data, dict):
        return False
    status = data.get("status")
    return status in {"completed", "failed", "killed", "error", "cancelled"}


class EventBus:
    def __init__(self, session_id: str, retention: int = 2000) -> None:
        self.session_id = session_id
        self._events: deque[EventEnvelope] = deque(maxlen=retention)
        self._tool_activity: dict[tuple[str, str], dict[str, Any]] = {}
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
            self._remember_tool_activity(envelope)
            for queue in tuple(self._subscribers):
                if not queue.full():
                    queue.put_nowait(envelope)
        return envelope

    def _remember_tool_activity(self, envelope: EventEnvelope) -> None:
        event_type = envelope.type
        data = envelope.data
        if event_type in {"job.started", "job.progress", "job.completed"}:
            call_id, actor_id = data.get("tool_call_id"), data.get("id")
            fields = ("tool_call_id", "id", "status", "lines_read", "returncode")
            snapshot = {key: data[key] for key in fields if key in data}
        elif event_type in {"subagent.start", "subagent.event", "subagent.end"}:
            call_id, actor_id = data.get("task_call_id"), data.get("session_id")
            snapshot = {
                "task_call_id": call_id,
                "session_id": actor_id,
                "subagent": str(data.get("subagent") or "Subagent")[:80],
            }
            if event_type == "subagent.event":
                nested_type, nested = data.get("event"), data.get("data")
                if not isinstance(nested, dict):
                    return
                if nested_type == "tool.start":
                    snapshot.update(
                        event=nested_type, data={"name": str(nested.get("name") or "Tool")[:80]}
                    )
                elif nested_type == "message.start" and nested.get("role") == "assistant":
                    snapshot.update(event=nested_type, data={"role": "assistant"})
                elif nested_type in {"job.started", "job.progress", "job.completed"}:
                    snapshot.update(
                        event=nested_type,
                        data={
                            "status": nested.get("status"),
                            "lines_read": nested.get("lines_read"),
                            "returncode": nested.get("returncode"),
                        },
                    )
                else:
                    return
            elif event_type == "subagent.end":
                snapshot["status"] = data.get("status") or (
                    "error" if data.get("error") else "completed"
                )
        else:
            return
        if (
            not isinstance(call_id, str)
            or not isinstance(actor_id, str)
            or not call_id
            or not actor_id
        ):
            return
        key = (call_id, actor_id)
        self._tool_activity.pop(key, None)
        self._tool_activity[key] = {"seq": envelope.seq, "type": event_type, "data": snapshot}
        if len(self._tool_activity) > _TOOL_ACTIVITY_HISTORY:
            self._evict_tool_activity()

    def _evict_tool_activity(self) -> None:
        terminal = (
            key for key, entry in self._tool_activity.items() if _is_terminal_activity(entry)
        )
        victim = next(terminal, None)
        if victim is None:
            victim = next(iter(self._tool_activity))
        self._tool_activity.pop(victim, None)

    def tool_activity(self) -> list[dict[str, Any]]:
        """Return compact transient progress, independent of SSE replay retention."""
        return list(self._tool_activity.values())

    def history(self, after: int = 0) -> list[EventEnvelope]:
        return [event for event in self._events if event.seq > after]

    def run_phase(self) -> dict[str, str] | None:
        for event in reversed(self._events):
            if event.type == "verifier":
                phase = {
                    "baseline": "checking baseline",
                    "running": "running checks",
                    "retry": "repairing failed checks",
                }.get(str(event.data.get("status")))
                return {"label": phase, "since": event.ts} if phase else None
            if event.type == "status":
                if event.data.get("phase") == "finishing_hooks":
                    return {"label": "finishing hooks", "since": event.ts}
                return None
            if event.type == "run.end":
                return None
        return None

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
