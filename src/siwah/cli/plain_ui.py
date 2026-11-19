"""Plain terminal renderer and permission prompts for `siwah run`."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from typing import Any

from ..permissions.policy import Approval, PermissionRequest


async def _read_input(prompt: str) -> str:
    """Do not let an uncancellable terminal read hold asyncio's shutdown executor."""

    loop = asyncio.get_running_loop()
    answer: asyncio.Future[str] = loop.create_future()

    def deliver(value: str | None, error: Exception | None) -> None:
        if answer.done():
            return
        if error is not None:
            answer.set_exception(error)
        else:
            answer.set_result(value or "")

    def read() -> None:
        try:
            value, error = input(prompt), None
        except Exception as exc:
            value, error = None, exc
        try:
            loop.call_soon_threadsafe(deliver, value, error)
        except RuntimeError:
            # The CLI may already have exited after cancellation.
            return

    threading.Thread(target=read, name="siwah-terminal-input", daemon=True).start()
    return await answer


class PlainRenderer:
    def __init__(self, *, show_reasoning: bool = False) -> None:
        self.show_reasoning = show_reasoning
        self._line_open = False

    async def emit(self, event_type: str, data: dict[str, Any]) -> None:
        if event_type == "message.delta":
            text = str(data.get("text", ""))
            if text:
                self._write(text)
                self._line_open = True
        elif event_type == "reasoning.delta" and self.show_reasoning:
            self._write(str(data.get("text", "")))
            self._line_open = True
        elif event_type == "tool.start":
            self._newline()
            self._write(f"[tool] {data.get('name')} {data.get('arguments_summary', '')}\n")
        elif event_type == "tool.end":
            status = data.get("status")
            if status == "error":
                self._newline()
                self._write(f"[tool error] {data.get('name')}: {data.get('error')}\n")
        elif event_type == "permission.request":
            self._newline()
            self._write(f"[permission] {data.get('tool')}: {data.get('preview')}\n")
        elif event_type == "error":
            self._newline()
            self._write(f"[error] {data.get('message')}\n", stream=sys.stderr)
        elif event_type == "run.end":
            self._newline()

    async def ask_permission(self, request: PermissionRequest, request_id: str) -> Approval:
        prompt = f"Allow {request.tool}? [o]nce/[s]ession/[a]lways/[d]eny [default: deny]: "
        try:
            answer = (await _read_input(prompt)).strip().lower()
        except EOFError:
            return Approval(decision="deny", reason="No interactive input")
        mapping = {"o": "once", "s": "session", "a": "always", "d": "deny"}
        decision = mapping.get(answer, "deny")
        return Approval(decision=decision)  # type: ignore[arg-type]

    async def ask_user(self, question: str) -> str:
        try:
            return await _read_input(f"{question}\n> ")
        except EOFError:
            return ""

    def _write(self, value: str, *, stream=None) -> None:
        target = stream or sys.stdout
        try:
            target.write(value)
            target.flush()
        except UnicodeEncodeError:
            encoding = getattr(target, "encoding", None) or "utf-8"
            target.write(value.encode(encoding, errors="replace").decode(encoding))
            target.flush()

    def _newline(self) -> None:
        if self._line_open:
            self._write("\n")
            self._line_open = False


class JsonRenderer:
    async def emit(self, event_type: str, data: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps({"event": event_type, "data": data}, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    async def ask_permission(self, request: PermissionRequest, request_id: str) -> Approval:
        return Approval(decision="deny", reason="Non-interactive JSON run; use --permission")

    async def ask_user(self, question: str) -> str:
        raise RuntimeError("ask_user is unavailable in non-interactive JSON mode")
