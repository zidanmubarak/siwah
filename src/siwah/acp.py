"""Agent Client Protocol (ACP) stdio bridge.

Implements the v1 baseline subset used by editors: initialize, session/new,
session/prompt, session/cancel, and session/update notifications. Requests that
need interactive approval fail closed until client permission RPC is supported.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from .auth import AuthStore
from .config import Config
from .core.loop import AgentLoop, RunResult
from .core.storage import Session, Store
from .providers.registry import ProviderRegistry, RoleRouter
from .tools.base import ToolRegistry
from .tools.runtime import default_tools
from .version import __version__

EventSink = Callable[[str, dict[str, Any]], Awaitable[None]]
Runner = Callable[[Session, str, EventSink], Awaitable[RunResult]]


class AcpError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class AcpServer:
    def __init__(
        self,
        config: Config,
        store: Store,
        *,
        runner: Runner | None = None,
        write: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self._runner = runner
        self._write = write or self._stdout_write
        self._sessions: dict[str, Session] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._replies: set[asyncio.Task[Any]] = set()
        self._initialized = False

    async def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        request_id = message.get("id")
        raw_params = message.get("params")
        params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
        if method == "initialize":
            self._initialized = True
            return self._result(
                request_id,
                {
                    "protocolVersion": 1,
                    "agentCapabilities": {
                        "loadSession": False,
                        "promptCapabilities": {
                            "image": False,
                            "audio": False,
                            "embeddedContext": False,
                        },
                        "mcpCapabilities": {"http": False, "sse": False},
                    },
                    "agentInfo": {"name": "siwah", "title": "siwah", "version": __version__},
                    "authMethods": [],
                },
            )
        if method == "session/new":
            if not self._initialized:
                return self._error(request_id, -32000, "initialize required")
            cwd = str(params.get("cwd", ""))
            if not cwd or not (cwd.startswith("/") or (len(cwd) > 2 and cwd[1] == ":")):
                return self._error(request_id, -32602, "cwd must be absolute")
            model = self.config.roles.main
            session = self.store.create_session(cwd=cwd, model=model, title="acp")
            self._sessions[session.id] = session
            return self._result(request_id, {"sessionId": session.id})
        if method == "session/prompt":
            if not self._initialized:
                return self._error(request_id, -32000, "initialize required")
            session_id = str(params.get("sessionId", ""))
            session = self._sessions.get(session_id) or self.store.get_session(session_id)
            if session is None:
                return self._error(request_id, -32002, "unknown session")
            text = _prompt_text(params.get("prompt"))
            if not text:
                return self._error(request_id, -32602, "prompt must contain text")
            return await self._prompt(session, text, request_id)
        if method == "session/cancel":
            session_id = str(params.get("sessionId", ""))
            task = self._tasks.get(session_id)
            if task is not None:
                task.cancel()
            return None
        if method == "session/load":
            return self._error(request_id, -32601, "session/load is not supported")
        if method in {"shutdown", "exit"}:
            return self._result(request_id, {})
        return self._error(request_id, -32601, f"unknown method: {method}")

    async def serve(self, *, stdin: Any = None, stdout: Any = None) -> None:
        input_stream = stdin or sys.stdin
        output_stream = stdout or sys.stdout
        while True:
            line = await asyncio.to_thread(input_stream.readline)
            if not line:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("method") == "session/prompt":
                # Keep reading stdin while the turn runs so session/cancel can
                # reach the task that is actually executing it.
                task = asyncio.create_task(self._reply_to(message, output_stream))
                self._replies.add(task)
                task.add_done_callback(self._replies.discard)
                continue
            response = await self.handle(message)
            if response is not None:
                output_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
                output_stream.flush()
        # EOF must not discard responses for prompts already admitted.
        if self._replies:
            await asyncio.gather(*self._replies)

    async def _reply_to(self, message: dict[str, Any], output_stream: Any) -> None:
        response: dict[str, Any] | None = None
        try:
            response = await self.handle(message)
        except asyncio.CancelledError:
            response = self._result(message.get("id"), {"stopReason": "cancelled"})
        except Exception as exc:  # keep the JSON-RPC stream alive
            print(f"acp: prompt failed: {exc}", file=sys.stderr, flush=True)
            response = self._error(message.get("id"), -32603, f"internal error: {exc}")
        if response is not None:
            output_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
            output_stream.flush()

    async def _prompt(self, session: Session, text: str, request_id: Any) -> dict[str, Any] | None:
        existing = self._tasks.get(session.id)
        if existing is not None and not existing.done():
            return self._error(request_id, -32000, "A prompt is already active for this session")

        async def emit(event: str, data: dict[str, Any]) -> None:
            update = _acp_update(event, data)
            if update is not None:
                self._write(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {"sessionId": session.id, "update": update},
                    }
                )

        task = asyncio.create_task(self._run(session, text, emit))
        self._tasks[session.id] = task
        try:
            result = await task
        except asyncio.CancelledError:
            return self._result(request_id, {"stopReason": "cancelled"})
        finally:
            if self._tasks.get(session.id) is task:
                self._tasks.pop(session.id, None)
        stop = "end_turn" if result.error is None else "max_turn_requests"
        return self._result(request_id, {"stopReason": stop})

    async def _run(self, session: Session, text: str, emit: EventSink) -> RunResult:
        if self._runner is not None:
            return await self._runner(session, text, emit)
        registry = ProviderRegistry(self.config, AuthStore())
        router = RoleRouter(self.config, registry)
        loop = AgentLoop(
            config=self.config,
            store=self.store,
            router=router,
            tools=ToolRegistry(default_tools()),
        )

        async def approve(_request, _request_id):
            from .permissions.policy import Approval

            return Approval(
                decision="deny",
                reason="ACP client permission requests are not supported; use the TUI to approve",
            )

        try:
            return await loop.run(
                session,
                text,
                emit=emit,
                permission_resolver=approve,
                model_override=session.model,
                extend_budget=True,
            )
        finally:
            await loop.close_jobs()
            await registry.aclose()

    def _result(self, request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _error(self, request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    @staticmethod
    def _stdout_write(payload: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def _prompt_text(prompt: Any) -> str:
    if not isinstance(prompt, list):
        return ""
    parts: list[str] = []
    for block in prompt:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "\n".join(part for part in parts if part)


def _acp_update(event: str, data: dict[str, Any]) -> dict[str, Any] | None:
    if event == "message.delta" and data.get("text"):
        return {
            "sessionUpdate": "agent_message_chunk",
            "messageId": data.get("message_id"),
            "content": {"type": "text", "text": str(data["text"])},
        }
    if event == "reasoning.delta" and data.get("text"):
        return {
            "sessionUpdate": "agent_thought_chunk",
            "content": {"type": "text", "text": str(data["text"])},
        }
    if event == "tool.start":
        return {
            "sessionUpdate": "tool_call",
            "toolCallId": data.get("tool_call_id"),
            "title": str(data.get("name", "tool")),
            "kind": "other",
            "status": "pending",
        }
    if event == "tool.end":
        return {
            "sessionUpdate": "tool_call_update",
            "toolCallId": data.get("tool_call_id"),
            "status": "completed" if data.get("status") == "ok" else "failed",
        }
    return None


__all__ = ["AcpError", "AcpServer"]
