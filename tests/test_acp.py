from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from siwah.acp import AcpServer
from siwah.config import Config
from siwah.core.loop import RunResult
from siwah.core.storage import Store


@pytest.mark.asyncio
async def test_acp_initialize_new_and_prompt(tmp_path: Path):
    store = Store(tmp_path / "state.db")
    notifications: list[dict[str, Any]] = []

    async def runner(session, text, emit):
        await emit("message.delta", {"text": f"echo {text}", "message_id": "m1"})
        return RunResult(session_id=session.id, text=f"echo {text}")

    server = AcpServer(
        Config(),
        store,
        runner=runner,
        write=notifications.append,
    )
    try:
        initialized = await server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}}
        )
        assert initialized is not None
        assert initialized["result"]["protocolVersion"] == 1

        created = await server.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/new",
                "params": {"cwd": str(tmp_path), "mcpServers": []},
            }
        )
        assert created is not None
        session_id = created["result"]["sessionId"]

        prompted = await server.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": "hello"}],
                },
            }
        )
        assert prompted is not None
        assert prompted["result"]["stopReason"] == "end_turn"
        assert notifications[0]["method"] == "session/update"
        assert notifications[0]["params"]["update"]["content"]["text"] == "echo hello"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_acp_rejects_relative_cwd(tmp_path: Path):
    store = Store(tmp_path / "state.db")
    server = AcpServer(Config(), store)
    try:
        await server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        error = await server.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/new",
                "params": {"cwd": "relative", "mcpServers": []},
            }
        )
        assert error is not None and error["error"]["code"] == -32602
    finally:
        store.close()
