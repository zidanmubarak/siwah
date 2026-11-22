from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from siwah.mcp.client import HttpMcpClient, McpError, normalize_server
from siwah.mcp.oauth import (
    McpAuthRequired,
    OAuthClient,
    OAuthState,
    oauth_status,
    save_state,
)


def _oauth_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/.well-known/oauth-protected-resource":
        return httpx.Response(
            200,
            json={
                "resource": "https://mcp.example.com/mcp",
                "authorization_servers": ["https://auth.example.com"],
            },
        )
    if path == "/.well-known/oauth-authorization-server":
        return httpx.Response(
            200,
            json={
                "authorization_endpoint": "https://auth.example.com/authorize",
                "token_endpoint": "https://auth.example.com/token",
                "registration_endpoint": "https://auth.example.com/register",
                "device_authorization_endpoint": "https://auth.example.com/device",
                "code_challenge_methods_supported": ["S256"],
                "scopes_supported": ["profile", "email"],
            },
        )
    if path == "/register":
        return httpx.Response(200, json={"client_id": "client-123"})
    if path == "/token":
        form = parse_qs(request.content.decode())
        if form.get("grant_type") == ["refresh_token"]:
            return httpx.Response(
                200,
                json={
                    "access_token": "refreshed-token",
                    "refresh_token": "refresh-2",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        return httpx.Response(
            200,
            json={
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "expires_in": 3600,
                "token_type": "Bearer",
                "scope": "profile",
            },
        )
    return httpx.Response(404)


def test_normalize_server_accepts_generic_oauth_config():
    config = normalize_server(
        "context7",
        {
            "transport": "http",
            "url": "https://mcp.context7.com/mcp",
            "auth": "oauth",
            "scopes": ["profile", "email"],
            "client_id": "client-123",
        },
    )

    assert config["auth"] == "oauth"
    assert config["scopes"] == ["profile", "email"]
    assert config["client_id"] == "client-123"


def test_normalize_server_rejects_static_auth_with_oauth():
    with pytest.raises(McpError, match="static Authorization"):
        normalize_server(
            "x",
            {
                "transport": "http",
                "url": "https://example.com/mcp",
                "auth": "oauth",
                "headers": {"Authorization": "Bearer old"},
            },
        )


async def test_discovery_registration_and_code_exchange(monkeypatch, tmp_path):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path))
    client = OAuthClient(
        "demo",
        "https://mcp.example.com/mcp",
        transport=httpx.MockTransport(_oauth_handler),
    )
    metadata = await client.discover()

    assert metadata.token_endpoint == "https://auth.example.com/token"
    await client._ensure_client("http://127.0.0.1:4444/callback")
    assert client.state.client_id == "client-123"

    await client._exchange_code(
        metadata,
        code="code-1",
        redirect_uri="http://127.0.0.1:4444/callback",
        verifier="verifier",
    )

    assert await client.access_token() == "access-1"
    assert oauth_status("demo") == "authenticated"
    await client.aclose()


async def test_authorization_code_flow_with_loopback_callback(monkeypatch, tmp_path):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path))
    notices: list[str] = []

    def open_browser(url: str) -> bool:
        query = parse_qs(urlsplit(url).query)
        redirect_uri = query["redirect_uri"][0]
        state = query["state"][0]
        response = httpx.get(
            redirect_uri,
            params={"code": "code-1", "state": state},
            trust_env=False,
            timeout=5,
        )
        assert response.status_code == 200
        return True

    client = OAuthClient(
        "demo-browser",
        "https://mcp.example.com/mcp",
        transport=httpx.MockTransport(_oauth_handler),
        notify=notices.append,
        open_browser=open_browser,
    )

    await client.authorize()

    assert await client.access_token() == "access-1"
    assert any("authorize" in notice for notice in notices)
    await client.aclose()


async def test_refresh_uses_the_stored_refresh_token(monkeypatch, tmp_path):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path))
    save_state(
        OAuthState(
            server="demo-refresh",
            resource="https://mcp.example.com/mcp",
            client_id="client-123",
            token_endpoint="https://auth.example.com/token",
            access_token="expired",
            refresh_token="refresh-1",
            expires_at=0.0,
        )
    )
    client = OAuthClient(
        "demo-refresh",
        "https://mcp.example.com/mcp",
        transport=httpx.MockTransport(_oauth_handler),
    )

    assert await client.access_token() == "refreshed-token"
    assert client.state.refresh_token == "refresh-2"
    await client.aclose()


async def test_http_client_retries_after_401_with_refreshed_token(monkeypatch, tmp_path):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path))
    save_state(
        OAuthState(
            server="demo-http",
            resource="https://mcp.example.com/mcp",
            client_id="client-123",
            token_endpoint="https://auth.example.com/token",
            access_token="old-token",
            refresh_token="refresh-1",
            expires_at=None,
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return _oauth_handler(request)
        if request.headers.get("authorization") != "Bearer refreshed-token":
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource"'
                },
            )
        payload = json.loads(request.content)
        if payload.get("method") == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"protocolVersion": "2025-06-18"},
                },
            )
        if payload.get("method") == "notifications/initialized":
            return httpx.Response(202)
        if payload.get("method") == "tools/list":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "tools": [
                            {
                                "name": "lookup",
                                "description": "Lookup docs",
                                "inputSchema": {"type": "object"},
                            }
                        ]
                    },
                },
            )
        return httpx.Response(404)

    oauth = OAuthClient(
        "demo-http",
        "https://mcp.example.com/mcp",
        transport=httpx.MockTransport(handler),
    )
    client = HttpMcpClient(
        "https://mcp.example.com/mcp",
        oauth=oauth,
        transport=httpx.MockTransport(handler),
    )
    try:
        tools = await client.list_tools()
    finally:
        await client.close()

    assert [tool.name for tool in tools] == ["lookup"]
    assert oauth.state.access_token == "refreshed-token"


def test_missing_oauth_state_is_reported(monkeypatch, tmp_path):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path))
    assert oauth_status("never-configured") == "missing"


async def test_access_token_without_refresh_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path))
    save_state(
        OAuthState(
            server="demo-empty",
            resource="https://mcp.example.com/mcp",
            access_token="expired",
            expires_at=0.0,
        )
    )
    client = OAuthClient(
        "demo-empty",
        "https://mcp.example.com/mcp",
        transport=httpx.MockTransport(_oauth_handler),
    )

    with pytest.raises(McpAuthRequired, match="siwah mcp auth"):
        await client.access_token()
    await client.aclose()
