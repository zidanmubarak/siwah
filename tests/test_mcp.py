from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

from siwah.config import AgentConfig, PermissionsConfig
from siwah.mcp.client import (
    HttpMcpClient,
    McpCallResult,
    McpError,
    McpManager,
    McpToolInfo,
    load_mcp_config,
    mcp_context,
    normalize_server,
    save_mcp_config,
)
from siwah.permissions.policy import PermissionPolicy
from siwah.tools.base import ToolContext
from siwah.tools.mcp import McpTool


def _server_script(tmp_path: Path) -> Path:
    script = tmp_path / "mcp_server.py"
    script.write_text(
        """
import json
import os
import sys

for line in sys.stdin:
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        continue
    method = message.get("method")
    if method == "notifications/initialized":
        continue
    if "id" not in message:
        continue
    request_id = message["id"]
    params = message.get("params") or {}
    if method == "initialize":
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "1"},
        }
    elif method == "tools/list":
        if params.get("cursor") is None:
            result = {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo text",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                        },
                    }
                ],
                "nextCursor": "page-2",
            }
        else:
            result = {
                "tools": [
                    {
                        "name": "env",
                        "description": "Show env and cwd",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ]
            }
    elif method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "echo":
            result = {"content": [{"type": "text", "text": "echo:" + arguments.get("text", "")}]}
        elif name == "env":
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": "env:" + os.environ.get("AUDIT_VALUE", "") + ";cwd:" + os.getcwd(),
                    }
                ]
            }
        elif name == "fail":
            result = {
                "content": [{"type": "text", "text": "deliberate failure"}],
                "isError": True,
            }
        else:
            result = {"content": [{"type": "text", "text": "unknown"}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), flush=True)
""".lstrip(),
        encoding="utf-8",
    )
    return script


@pytest.mark.asyncio
async def test_stdio_mcp_lazy_tools_pagination_and_env(tmp_path: Path):
    harness = tmp_path / "harness"
    harness.mkdir()
    script = _server_script(tmp_path)
    (harness / "mcp.json").write_text(
        json.dumps(
            {
                "servers": {
                    "fake": {
                        "transport": "stdio",
                        "command": [sys.executable, str(script)],
                        "env": {"AUDIT_VALUE": "from-config"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    manager = McpManager.load(harness)
    try:
        tools = await manager.list_tools()
        assert [tool.name for tool in tools["fake"]] == ["echo", "env"]
        schema = await manager.schema("fake", "echo")
        assert schema["properties"]["text"]["type"] == "string"
        assert await manager.call("fake", "echo", {"text": "hi"}) == "echo:hi"
        env_text = await manager.call("fake", "env", {})
        assert "env:from-config" in env_text
        assert str(harness) in env_text
        result = await manager.call_result("fake", "fail", {})
        assert result.is_error is True
        assert result.text == "deliberate failure"
        statuses = await manager.status(refresh=True)
        assert statuses[0]["status"] == "connected"
        assert statuses[0]["tools"] == ["echo", "env"]
        restarted = await manager.restart("fake")
        assert restarted["status"] == "connected"
        assert restarted["tools"] == ["echo", "env"]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_http_mcp_json_and_initialized_notification():
    methods: list[str] = []
    protocol_headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)
        method = message["method"]
        methods.append(method)
        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        protocol_headers.append(request.headers.get("MCP-Protocol-Version"))
        if method == "tools/list":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {"tools": [{"name": "echo", "description": "Echo"}]},
                },
            )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {"content": [{"type": "text", "text": "ok"}]},
            },
        )

    client = HttpMcpClient("http://test/mcp", transport=httpx.MockTransport(handler))
    try:
        tools = await client.list_tools()
        assert tools[0].name == "echo"
        assert await client.call_tool("echo", {}) == "ok"
    finally:
        await client.close()
    assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    assert protocol_headers == ["2025-06-18", "2025-06-18"]


@pytest.mark.asyncio
async def test_http_mcp_sse_response():
    def handler(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)
        method = message["method"]
        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {"protocolVersion": "2025-06-18", "capabilities": {}},
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {"tools": [{"name": "sse", "description": "SSE tool"}]},
            }
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=f"event: message\ndata: {payload}\n\n",
        )

    client = HttpMcpClient("http://test/mcp", transport=httpx.MockTransport(handler))
    try:
        tools = await client.list_tools()
        assert [tool.name for tool in tools] == ["sse"]
    finally:
        await client.close()


def test_normalize_server_validates_transport_and_url():
    with pytest.raises(McpError):
        normalize_server("bad", {"transport": "carrier-pigeon"})
    with pytest.raises(McpError):
        normalize_server("bad", {"transport": "http", "url": "ftp://example"})
    with pytest.raises(McpError):
        normalize_server("bad", {"transport": "stdio", "command": []})
    normalized = normalize_server(
        "remote",
        {"transport": "streamable-http", "url": "https://example.test/mcp", "headers": {"A": "B"}},
    )
    assert normalized["transport"] == "http"
    assert normalized["headers"] == {"A": "B"}


def test_mcp_config_roundtrip_and_context_redaction(tmp_path: Path):
    harness = tmp_path / "harness"
    payload = {
        "servers": {
            "remote": {
                "transport": "http",
                "url": "https://example.test/mcp",
                "headers": {"Authorization": "Bearer SECRET"},
                "description": "Remote tools",
            }
        }
    }
    save_mcp_config(payload, harness)
    assert load_mcp_config(harness) == payload
    text = mcp_context(harness)
    assert "remote (http)" in text
    assert "Remote tools" in text
    assert "SECRET" not in text
    (harness / "mcp.json").write_text("{bad json", encoding="utf-8")
    with pytest.raises(McpError):
        load_mcp_config(harness, strict=True)


@pytest.mark.asyncio
async def test_mcp_tool_list_and_call(tmp_path: Path):
    class FakeManager:
        async def list_tools_report(self, server=None):
            return {"fake": [McpToolInfo("echo", "Echo text", {})]}, {}

        async def schema(self, server, tool):
            return {"type": "object"}

        async def call_result(self, server, tool, arguments):
            return McpCallResult(f"echo:{arguments.get('text', '')}")

    context = ToolContext(
        workspace=tmp_path,
        policy=PermissionPolicy(tmp_path, PermissionsConfig(mode="yolo")),
        session_id="ses",
        agent_config=AgentConfig(),
        mcp=FakeManager(),
    )
    tool = McpTool()
    listed = await tool.run(context, action="list")
    assert "fake/echo: Echo text" in listed.output
    called = await tool.run(
        context, action="call", server="fake", tool="echo", arguments={"text": "hi"}
    )
    assert called.output == "echo:hi"


def test_mcp_cli_add_list_test_show_remove(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from typer.testing import CliRunner

    from siwah.cli.main import app

    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    script = _server_script(tmp_path)
    runner = CliRunner()
    added = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "audit",
            "--command",
            f"{sys.executable} {script}",
            "--env",
            "AUDIT_VALUE=from-cli",
            "--description",
            "Audit server",
        ],
    )
    assert added.exit_code == 0, added.output
    listed = runner.invoke(app, ["mcp", "list"])
    assert listed.exit_code == 0
    assert "audit" in listed.output
    tested = runner.invoke(app, ["mcp", "test", "audit"])
    assert tested.exit_code == 0, tested.output
    assert "audit/echo" in tested.output
    status = runner.invoke(app, ["mcp", "status"])
    assert status.exit_code == 0, status.output
    assert "connected" in status.output
    assert "audit" in status.output
    shown = runner.invoke(app, ["mcp", "show", "audit"])
    assert shown.exit_code == 0
    assert "from-cli" not in shown.output
    assert '"transport": "stdio"' in shown.output
    removed = runner.invoke(app, ["mcp", "remove", "audit"])
    assert removed.exit_code == 0
    empty = runner.invoke(app, ["mcp", "list"])
    assert "No MCP servers configured" in empty.output


@pytest.mark.asyncio
async def test_mcp_tool_add_status_remove(tmp_path: Path):
    harness = tmp_path / "harness"
    harness.mkdir()
    script = _server_script(tmp_path)
    manager = McpManager({}, root=harness)
    context = ToolContext(
        workspace=tmp_path,
        policy=PermissionPolicy(tmp_path, PermissionsConfig(mode="yolo")),
        session_id="ses",
        agent_config=AgentConfig(),
        mcp=manager,
    )
    tool = McpTool()
    try:
        added = await tool.run(
            context,
            action="add",
            server="audit",
            transport="stdio",
            command=f"{sys.executable} {script}",
            env={"AUDIT_VALUE": "from-tool"},
            description="Audit server",
        )
        assert added.ok, added.output
        status = await tool.run(context, action="status")
        assert status.ok
        assert "audit: connected" in status.output
        assert "2 tools" in status.output
        removed = await tool.run(context, action="remove", server="audit")
        assert removed.ok, removed.output
        payload = json.loads((harness / "mcp.json").read_text(encoding="utf-8"))
        assert "audit" not in payload["servers"]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_mcp_tool_reports_server_error(tmp_path: Path):
    class ErrorManager:
        async def list_tools_report(self, server=None):
            return {}, {"fake": "server unavailable"}

        async def schema(self, server, tool):
            raise McpError("server unavailable")

        async def call_result(self, server, tool, arguments):
            return McpCallResult("boom", is_error=True)

    context = ToolContext(
        workspace=tmp_path,
        policy=PermissionPolicy(tmp_path, PermissionsConfig(mode="yolo")),
        session_id="ses",
        agent_config=AgentConfig(),
        mcp=ErrorManager(),
    )
    tool = McpTool()
    listed = await tool.run(context, action="list")
    assert "fake: ERROR server unavailable" in listed.output
    called = await tool.run(context, action="call", server="fake", tool="echo")
    assert called.ok is False
    assert "boom" in called.output
