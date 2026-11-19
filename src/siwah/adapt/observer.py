"""Observe recent trajectories and extract evolution signals."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from ..kernel.secrets import redact_secrets
from .models import Observation

if TYPE_CHECKING:
    from ..core.storage import Store


class Observer:
    def __init__(self, *, text_limit: int = 4_000) -> None:
        self.text_limit = text_limit

    def observe(self, store: Store, *, limit: int = 20) -> list[Observation]:
        observations: list[Observation] = []
        for session in store.list_sessions(limit=limit):
            messages = store.get_messages(session.id)
            calls = store.get_tool_calls(session.id)
            usage = store.usage_totals(session.id)
            user_messages = [message.content for message in messages if message.role == "user"]
            assistant_messages = [
                message.content for message in messages if message.role == "assistant"
            ]
            corrections = _corrections(messages)
            text = "\n".join(item for item in user_messages if item)
            observations.append(
                Observation(
                    session_id=session.id,
                    model=session.model,
                    status=session.status,
                    user_messages=len(user_messages),
                    assistant_messages=len(assistant_messages),
                    tool_calls=len(calls),
                    tool_errors=sum(1 for call in calls if call.get("status") != "ok"),
                    corrections=corrections,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    allowance=usage.allowance,
                    duration_s=_duration(session.created_at, session.updated_at),
                    text=redact_secrets(text)[-self.text_limit :],
                )
            )
        return observations

    def signals(self, observations: list[Observation]) -> list[str]:
        signals: list[str] = []
        for item in observations:
            if item.tool_errors >= 3:
                signals.append(f"repeated tool errors in {item.session_id}")
            if item.corrections >= 2:
                signals.append(f"repeated user corrections in {item.session_id}")
            if item.status in {"aborted", "error"}:
                signals.append(f"unfinished session {item.session_id}: {item.status}")
        return signals


_CORRECTION_HINTS = (
    "wrong",
    "incorrect",
    "that's not",
    "that is not",
    "still fail",
    "try again",
    "not what",
    "no,",
    "bukan",
    "salah",
    "ulangi",
)


def _corrections(messages: list[Any]) -> int:
    """Count explicit correction turns, not every follow-up message."""

    corrections = 0
    previous_user = ""
    tool_error_since_user = False
    for message in messages:
        if message.role == "tool":
            content = (message.content or "").lower()
            if "error" in content or "failed" in content:
                tool_error_since_user = True
        elif message.role == "user":
            text = (message.content or "").strip()
            lowered = text.lower()
            repeated = bool(previous_user) and text == previous_user
            hinted = any(hint in lowered for hint in _CORRECTION_HINTS)
            if tool_error_since_user or repeated or hinted:
                corrections += 1
            previous_user = text
            tool_error_since_user = False
    return corrections


def _duration(start: str, end: str) -> float:
    from datetime import datetime

    try:
        first = datetime.fromisoformat(start)
        last = datetime.fromisoformat(end)
    except ValueError:
        return 0.0
    return max(0.0, (last - first).total_seconds())


def observation_payload(observations: list[Observation]) -> str:
    return json.dumps(
        [item.model_dump(mode="json") for item in observations],
        ensure_ascii=False,
        indent=2,
    )


__all__ = ["Observer", "observation_payload"]
