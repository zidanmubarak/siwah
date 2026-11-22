"""Generic OAuth 2.1 support for streamable HTTP MCP servers.

The flow is server-agnostic:

1. discover protected-resource metadata (RFC 9728), falling back to the
   ``WWW-Authenticate`` challenge from the MCP endpoint;
2. discover the authorization server (RFC 8414 / OpenID Connect);
3. register a public client dynamically when the server supports it;
4. use authorization code + PKCE in a browser, or the device flow for
   headless/SSH sessions;
5. store access/refresh tokens in ``~/.siwah/mcp-auth/<server>.json`` with mode
   600 and refresh them before expiry.

Nothing here is Context7-specific. A server that publishes standard OAuth
metadata works without a code change.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import secrets
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from ..paths import siwah_home, write_private

OAUTH_STATE_VERSION = 1
DEFAULT_TIMEOUT_S = 30.0
CALLBACK_TIMEOUT_S = 300.0
TOKEN_SKEW_S = 60.0
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class McpOAuthError(RuntimeError):
    pass


class McpAuthRequired(McpOAuthError):
    """The server needs a user login before it can be used."""


@dataclass(frozen=True)
class OAuthMetadata:
    resource: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None = None
    device_authorization_endpoint: str | None = None
    revocation_endpoint: str | None = None
    scopes: tuple[str, ...] = ()
    code_challenge_methods: tuple[str, ...] = ("S256",)


@dataclass
class OAuthState:
    server: str
    resource: str
    client_id: str | None = None
    client_secret: str | None = None
    scope: str = ""
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    registration_endpoint: str | None = None
    device_authorization_endpoint: str | None = None
    revocation_endpoint: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str = "Bearer"
    expires_at: float | None = None
    obtained_at: float | None = None
    version: int = OAUTH_STATE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "server": self.server,
            "resource": self.resource,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": self.scope,
            "authorization_endpoint": self.authorization_endpoint,
            "token_endpoint": self.token_endpoint,
            "registration_endpoint": self.registration_endpoint,
            "device_authorization_endpoint": self.device_authorization_endpoint,
            "revocation_endpoint": self.revocation_endpoint,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_at": self.expires_at,
            "obtained_at": self.obtained_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> OAuthState:
        return cls(
            server=str(payload.get("server", "")),
            resource=str(payload.get("resource", "")),
            client_id=_optional_str(payload.get("client_id")),
            client_secret=_optional_str(payload.get("client_secret")),
            scope=str(payload.get("scope", "")),
            authorization_endpoint=_optional_str(payload.get("authorization_endpoint")),
            token_endpoint=_optional_str(payload.get("token_endpoint")),
            registration_endpoint=_optional_str(payload.get("registration_endpoint")),
            device_authorization_endpoint=_optional_str(
                payload.get("device_authorization_endpoint")
            ),
            revocation_endpoint=_optional_str(payload.get("revocation_endpoint")),
            access_token=_optional_str(payload.get("access_token")),
            refresh_token=_optional_str(payload.get("refresh_token")),
            token_type=str(payload.get("token_type", "Bearer")),
            expires_at=_optional_float(payload.get("expires_at")),
            obtained_at=_optional_float(payload.get("obtained_at")),
            version=int(payload.get("version", OAUTH_STATE_VERSION)),
        )


def _optional_str(value: Any) -> str | None:
    return str(value) if isinstance(value, (str, int)) and str(value) else None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def oauth_dir() -> Path:
    path = siwah_home() / "mcp-auth"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_name(server: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in "._-" else "_" for character in server
    ).strip("._-")
    return cleaned[:80] or "server"


def oauth_state_path(server: str) -> Path:
    return oauth_dir() / f"{_safe_name(server)}.json"


def load_state(server: str) -> OAuthState | None:
    path = oauth_state_path(server)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise McpOAuthError(f"Cannot read OAuth state for {server}: {exc}") from exc
    if not isinstance(payload, dict):
        raise McpOAuthError(f"OAuth state for {server} is not an object")
    return OAuthState.from_dict(payload)


def save_state(state: OAuthState) -> Path:
    path = oauth_state_path(state.server)
    body = json.dumps(state.to_dict(), indent=2, ensure_ascii=False) + "\n"
    temporary = path.with_suffix(".tmp")
    write_private(temporary, body)
    temporary.replace(path)
    return path


def delete_state(server: str) -> bool:
    path = oauth_state_path(server)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def oauth_status(server: str) -> str:
    try:
        state = load_state(server)
    except McpOAuthError:
        return "invalid"
    if state is None:
        return "missing"
    if state.access_token and not _expired(state):
        return "authenticated"
    if state.refresh_token and state.token_endpoint:
        return "refreshable"
    if state.access_token:
        return "expired"
    return "missing"


def _expired(state: OAuthState, *, now: float | None = None) -> bool:
    if state.expires_at is None:
        return False
    return (now if now is not None else time.time()) >= state.expires_at - TOKEN_SKEW_S


def _validate_url(value: str, *, field_name: str = "URL") -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise McpOAuthError(f"{field_name} must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise McpOAuthError(f"{field_name} must not contain embedded credentials")
    if parsed.scheme == "http" and (parsed.hostname or "").lower() not in _LOOPBACK_HOSTS:
        raise McpOAuthError(f"{field_name} must use HTTPS (except loopback development servers)")
    return value.strip()


def _append_query(url: str, params: dict[str, str]) -> str:
    parsed = urlsplit(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    for key, value in params.items():
        query[key] = [value]
    encoded = urlencode([(key, item) for key, values in query.items() for item in values])
    return parsed._replace(query=encoded).geturl()


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _scope_string(scopes: list[str] | tuple[str, ...] | str | None) -> str:
    if isinstance(scopes, str):
        return scopes.strip()
    return " ".join(item.strip() for item in (scopes or ()) if item.strip())


def _metadata_from_state(state: OAuthState) -> OAuthMetadata | None:
    if not state.authorization_endpoint or not state.token_endpoint:
        return None
    return OAuthMetadata(
        resource=state.resource,
        authorization_endpoint=state.authorization_endpoint,
        token_endpoint=state.token_endpoint,
        registration_endpoint=state.registration_endpoint,
        device_authorization_endpoint=state.device_authorization_endpoint,
        revocation_endpoint=state.revocation_endpoint,
    )


class OAuthClient:
    def __init__(
        self,
        server: str,
        resource: str,
        *,
        client_id: str | None = None,
        client_secret_env: str | None = None,
        scopes: list[str] | tuple[str, ...] | str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        notify: Callable[[str], None] | None = None,
        open_browser: Callable[[str], bool] | None = None,
    ) -> None:
        self.server = server
        self.resource = _validate_url(resource, field_name="MCP server URL")
        self.client_id = client_id
        self.client_secret_env = client_secret_env
        self.scopes = (
            tuple(item.strip() for item in scopes if item.strip())
            if isinstance(scopes, (list, tuple))
            else (scopes.strip(),)
            if isinstance(scopes, str) and scopes.strip()
            else ()
        )
        self.transport = transport
        self.timeout_s = timeout_s
        self.notify = notify or (lambda _message: None)
        self.open_browser = open_browser or webbrowser.open
        self._http: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        existing = load_state(server)
        if existing is None or existing.resource != self.resource:
            self.state = OAuthState(server=server, resource=self.resource)
        else:
            self.state = existing
        if client_id:
            self.state.client_id = client_id
        self.state.scope = self.state.scope or _scope_string(self.scopes)

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                transport=self.transport,
                timeout=self.timeout_s,
                follow_redirects=True,
                headers={"User-Agent": "siwah/oauth"},
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _secret(self) -> str | None:
        if self.client_secret_env:
            return os.environ.get(self.client_secret_env) or None
        return self.state.client_secret

    async def _get_json(self, url: str) -> dict[str, Any] | None:
        _validate_url(url, field_name="OAuth metadata URL")
        response = await self._client().get(url, headers={"Accept": "application/json"})
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise McpOAuthError(f"OAuth metadata at {url} is not a JSON object")
        return payload

    async def _post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        _validate_url(url, field_name="OAuth registration URL")
        response = await self._client().post(
            url,
            json=payload,
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise McpOAuthError(f"OAuth response from {url} is not a JSON object")
        return body

    async def _post_form(
        self,
        url: str,
        data: dict[str, str],
        *,
        invalid_grant_is_login: bool = True,
    ) -> dict[str, Any]:
        _validate_url(url, field_name="OAuth token URL")
        response = await self._client().post(
            url,
            data=data,
            headers={"Accept": "application/json"},
        )
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400:
            error = str(body.get("error", "")) if isinstance(body, dict) else ""
            description = str(body.get("error_description", "")) if isinstance(body, dict) else ""
            detail = f"{error}: {description}".strip(": ") or response.text[:300]
            if error == "invalid_grant" and invalid_grant_is_login:
                raise McpAuthRequired(
                    f"OAuth authorization for {self.server} expired; "
                    f"run siwah mcp auth {self.server} ({detail})"
                )
            raise McpOAuthError(f"OAuth request failed ({response.status_code}): {detail}")
        if not isinstance(body, dict):
            raise McpOAuthError(f"OAuth token response from {url} is not a JSON object")
        return body

    async def _resource_metadata(self) -> dict[str, Any]:
        parsed = urlsplit(self.resource)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        candidates: list[str] = []
        if parsed.path and parsed.path != "/":
            candidates.append(f"{origin}/.well-known/oauth-protected-resource{parsed.path}")
        candidates.append(f"{origin}/.well-known/oauth-protected-resource")
        for candidate in candidates:
            try:
                payload = await self._get_json(candidate)
            except (httpx.HTTPError, McpOAuthError):
                continue
            if payload is not None and _authorization_servers(payload):
                return payload
        payload = await self._probe_mcp_challenge()
        if payload is not None:
            return payload
        raise McpOAuthError(f"{self.resource} did not advertise OAuth protected-resource metadata")

    async def _probe_mcp_challenge(self) -> dict[str, Any] | None:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "siwah", "version": "0.4"},
            },
        }
        response = await self._client().post(
            self.resource,
            json=payload,
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )
        if response.status_code != 401:
            return None
        challenge = response.headers.get("www-authenticate", "")
        resource_metadata = _www_authenticate_param(challenge, "resource_metadata")
        if not resource_metadata:
            return None
        _validate_url(resource_metadata, field_name="OAuth resource metadata URL")
        return await self._get_json(resource_metadata)

    async def discover(self) -> OAuthMetadata:
        cached = _metadata_from_state(self.state)
        if cached is not None:
            return cached
        resource_metadata = await self._resource_metadata()
        resource_scopes = resource_metadata.get("scopes_supported", [])
        if not isinstance(resource_scopes, list):
            resource_scopes = []
        auth_servers = _authorization_servers(resource_metadata)
        if not auth_servers:
            raise McpOAuthError("OAuth metadata has no authorization server")
        errors: list[str] = []
        for auth_server in auth_servers:
            try:
                metadata = await self._authorization_server_metadata(auth_server)
            except (httpx.HTTPError, McpOAuthError, ValueError) as exc:
                errors.append(str(exc))
                continue
            self.state.authorization_endpoint = metadata.authorization_endpoint
            self.state.token_endpoint = metadata.token_endpoint
            self.state.registration_endpoint = metadata.registration_endpoint
            self.state.device_authorization_endpoint = metadata.device_authorization_endpoint
            self.state.revocation_endpoint = metadata.revocation_endpoint
            if not self.state.scope:
                self.state.scope = _scope_string(resource_scopes or metadata.scopes)
            save_state(self.state)
            return metadata
        detail = errors[-1] if errors else "no usable authorization server"
        raise McpOAuthError(f"OAuth authorization server discovery failed: {detail}")

    async def _authorization_server_metadata(self, auth_server: str) -> OAuthMetadata:
        base = auth_server.rstrip("/")
        candidates = [
            f"{base}/.well-known/oauth-authorization-server",
            f"{base}/.well-known/openid-configuration",
        ]
        last_error: Exception | None = None
        for candidate in candidates:
            try:
                payload = await self._get_json(candidate)
            except (httpx.HTTPError, McpOAuthError, ValueError) as exc:
                last_error = exc
                continue
            if payload is None:
                continue
            authorization_endpoint = payload.get("authorization_endpoint")
            token_endpoint = payload.get("token_endpoint")
            if not isinstance(authorization_endpoint, str) or not isinstance(token_endpoint, str):
                continue
            methods = payload.get("code_challenge_methods_supported", [])
            method_list = tuple(str(item) for item in methods) if isinstance(methods, list) else ()
            if method_list and "S256" not in method_list:
                raise McpOAuthError("authorization server does not support PKCE S256")
            return OAuthMetadata(
                resource=self.resource,
                authorization_endpoint=_validate_url(
                    authorization_endpoint, field_name="OAuth authorization endpoint"
                ),
                token_endpoint=_validate_url(token_endpoint, field_name="OAuth token endpoint"),
                registration_endpoint=_optional_url(payload.get("registration_endpoint")),
                device_authorization_endpoint=_optional_url(
                    payload.get("device_authorization_endpoint")
                ),
                revocation_endpoint=_optional_url(payload.get("revocation_endpoint")),
                scopes=tuple(str(item) for item in payload.get("scopes_supported", []))
                if isinstance(payload.get("scopes_supported"), list)
                else (),
                code_challenge_methods=method_list or ("S256",),
            )
        raise McpOAuthError(
            f"authorization server metadata not found at {base}: {last_error or 'no endpoint'}"
        )

    async def _ensure_client(self, redirect_uri: str) -> None:
        if self.state.client_id:
            return
        if not self.state.registration_endpoint:
            raise McpOAuthError(
                f"{self.server} has no dynamic client registration; set client_id in mcp.json"
            )
        payload = {
            "client_name": "siwah",
            "redirect_uris": [redirect_uri],
            "grant_types": [
                "authorization_code",
                "refresh_token",
                "urn:ietf:params:oauth:grant-type:device_code",
            ],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        if self.state.scope:
            payload["scope"] = self.state.scope
        response = await self._post_json(self.state.registration_endpoint, payload)
        client_id = response.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            raise McpOAuthError("dynamic client registration did not return client_id")
        self.state.client_id = client_id
        if isinstance(response.get("client_secret"), str):
            self.state.client_secret = response["client_secret"]
        save_state(self.state)

    def _authorization_url(
        self,
        metadata: OAuthMetadata,
        *,
        redirect_uri: str,
        state: str,
        challenge: str,
    ) -> str:
        params = {
            "response_type": "code",
            "client_id": self.state.client_id or "",
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": self.resource,
        }
        if self.state.scope:
            params["scope"] = self.state.scope
        return _append_query(metadata.authorization_endpoint, params)

    async def _authorization_code(self, metadata: OAuthMetadata, *, no_browser: bool) -> None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, str]] = loop.create_future()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                request_line = await reader.readline()
                if not request_line:
                    return
                parts = request_line.decode("latin-1", errors="replace").split()
                path = parts[1] if len(parts) > 1 else "/"
                while True:
                    line = await reader.readline()
                    if line in {b"\r\n", b"\n", b""}:
                        break
                query = parse_qs(urlsplit(path).query)
                values = {key: item[0] for key, item in query.items() if item}
                body = b"siwah OAuth callback received. You can close this window.\n"
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\n"
                    + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                    + body
                )
                await writer.drain()
                if not future.done():
                    future.set_result(values)
            finally:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        sockets = server.sockets or []
        if not sockets:
            server.close()
            raise McpOAuthError("could not start the OAuth callback listener")
        port = int(sockets[0].getsockname()[1])
        redirect_uri = f"http://127.0.0.1:{port}/callback"
        try:
            await self._ensure_client(redirect_uri)
            verifier, challenge = _pkce()
            state = secrets.token_urlsafe(32)
            url = self._authorization_url(
                metadata,
                redirect_uri=redirect_uri,
                state=state,
                challenge=challenge,
            )
            self.notify(f"Open this URL to authorize {self.server}:\n{url}")
            if not no_browser:
                # The URL was printed above; a machine without a browser can
                # still complete the flow manually.
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self.open_browser, url)
            try:
                values = await asyncio.wait_for(future, timeout=CALLBACK_TIMEOUT_S)
            except TimeoutError as exc:
                raise McpOAuthError("OAuth browser callback timed out") from exc
            if values.get("error"):
                raise McpOAuthError(
                    f"OAuth authorization failed: {values.get('error_description') or values['error']}"
                )
            if values.get("state") != state:
                raise McpOAuthError("OAuth callback state mismatch")
            code = values.get("code")
            if not code:
                raise McpOAuthError("OAuth callback did not include a code")
            await self._exchange_code(
                metadata, code=code, redirect_uri=redirect_uri, verifier=verifier
            )
        finally:
            server.close()
            await server.wait_closed()

    async def _exchange_code(
        self,
        metadata: OAuthMetadata,
        *,
        code: str,
        redirect_uri: str,
        verifier: str,
    ) -> None:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self.state.client_id or "",
            "code_verifier": verifier,
            "resource": self.resource,
        }
        secret = self._secret()
        if secret:
            data["client_secret"] = secret
        payload = await self._post_form(metadata.token_endpoint, data)
        self._save_token_response(payload)

    async def _device_flow(self, metadata: OAuthMetadata) -> None:
        if not metadata.device_authorization_endpoint:
            raise McpOAuthError("authorization server does not support the device flow")
        await self._ensure_client("http://127.0.0.1/callback")
        data = {"client_id": self.state.client_id or ""}
        if self.state.scope:
            data["scope"] = self.state.scope
        data["resource"] = self.resource
        start = await self._post_form(
            metadata.device_authorization_endpoint,
            data,
            invalid_grant_is_login=False,
        )
        device_code = start.get("device_code")
        user_code = start.get("user_code")
        verification_uri = start.get("verification_uri")
        complete_uri = start.get("verification_uri_complete")
        if not all(
            isinstance(item, str) and item for item in (device_code, user_code, verification_uri)
        ):
            raise McpOAuthError("device authorization response is incomplete")
        device_code = str(device_code)
        user_code = str(user_code)
        verification_uri = str(verification_uri)
        self.notify(
            f"Authorize {self.server} at {verification_uri}\n"
            f"Enter code: {user_code}"
            + (f"\nDirect link: {complete_uri}" if isinstance(complete_uri, str) else "")
        )
        interval = max(1.0, float(start.get("interval", 5)))
        deadline = time.monotonic() + float(start.get("expires_in", 600))
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            try:
                payload = await self._post_form(
                    metadata.token_endpoint,
                    {
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        "device_code": device_code,
                        "client_id": self.state.client_id or "",
                        "resource": self.resource,
                    },
                    invalid_grant_is_login=False,
                )
            except McpOAuthError as exc:
                message = str(exc)
                if "authorization_pending" in message:
                    continue
                if "slow_down" in message:
                    interval += 5
                    continue
                raise
            if payload.get("access_token"):
                self._save_token_response(payload)
                return
        raise McpOAuthError("device authorization expired")

    def _save_token_response(self, payload: dict[str, Any]) -> None:
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise McpOAuthError("OAuth token response did not include access_token")
        self.state.access_token = access_token
        refresh_token = payload.get("refresh_token")
        if isinstance(refresh_token, str) and refresh_token:
            self.state.refresh_token = refresh_token
        token_type = payload.get("token_type")
        if isinstance(token_type, str) and token_type:
            self.state.token_type = token_type
        scope = payload.get("scope")
        if isinstance(scope, str) and scope:
            self.state.scope = scope
        expires_in = payload.get("expires_in")
        try:
            expires = float(expires_in) if expires_in is not None else None
        except (TypeError, ValueError):
            expires = None
        self.state.obtained_at = time.time()
        self.state.expires_at = self.state.obtained_at + expires if expires else None
        save_state(self.state)

    async def refresh(self) -> str:
        async with self._lock:
            if not self.state.refresh_token or not self.state.token_endpoint:
                raise McpAuthRequired(
                    f"OAuth authentication required for {self.server}; "
                    f"run siwah mcp auth {self.server}"
                )
            data = {
                "grant_type": "refresh_token",
                "refresh_token": self.state.refresh_token,
                "client_id": self.state.client_id or "",
                "resource": self.resource,
            }
            secret = self._secret()
            if secret:
                data["client_secret"] = secret
            payload = await self._post_form(self.state.token_endpoint, data)
            self._save_token_response(payload)
            if not self.state.access_token:
                raise McpOAuthError("OAuth refresh did not return an access token")
            return self.state.access_token

    async def access_token(self) -> str:
        if self.state.access_token and not _expired(self.state):
            return self.state.access_token
        if self.state.refresh_token and self.state.token_endpoint:
            return await self.refresh()
        raise McpAuthRequired(
            f"OAuth authentication required for {self.server}; run siwah mcp auth {self.server}"
        )

    async def authorize(self, *, device: bool = False, no_browser: bool = False) -> OAuthState:
        metadata = await self.discover()
        if device:
            await self._device_flow(metadata)
        else:
            await self._authorization_code(metadata, no_browser=no_browser)
        return self.state

    async def logout(self) -> None:
        token = self.state.refresh_token or self.state.access_token
        if token and self.state.revocation_endpoint:
            with contextlib.suppress(httpx.HTTPError, McpOAuthError, ValueError):
                await self._post_form(
                    self.state.revocation_endpoint,
                    {
                        "token": token,
                        "token_type_hint": "refresh_token"
                        if self.state.refresh_token
                        else "access_token",
                        "client_id": self.state.client_id or "",
                    },
                )
        delete_state(self.server)
        self.state.access_token = None
        self.state.refresh_token = None
        self.state.expires_at = None


def _authorization_servers(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("authorization_servers")
    if isinstance(raw, list):
        return [str(item) for item in raw if isinstance(item, str) and item]
    single = payload.get("authorization_server")
    return [str(single)] if isinstance(single, str) and single else []


def _optional_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return _validate_url(value, field_name="OAuth endpoint")


def _www_authenticate_param(header: str, name: str) -> str | None:
    import re

    match = re.search(rf'{re.escape(name)}=(?:"([^"]+)"|([^\s,]+))', header)
    if match is None:
        return None
    return match.group(1) or match.group(2)


def state_summary(server: str) -> str:
    return oauth_status(server)


__all__ = [
    "McpAuthRequired",
    "McpOAuthError",
    "OAuthClient",
    "OAuthMetadata",
    "OAuthState",
    "delete_state",
    "load_state",
    "oauth_state_path",
    "oauth_status",
    "save_state",
    "state_summary",
]
