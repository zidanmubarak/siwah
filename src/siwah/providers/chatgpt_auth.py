"""Open-source Sign in with ChatGPT using loopback OAuth and rotating credentials."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import os
import secrets
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import httpx
from filelock import FileLock, Timeout
from pydantic import BaseModel, Field, ValidationError

from ..auth import AuthError
from ..paths import siwah_home

_ISSUER = "https://auth.openai.com"
_DISCOVERY = f"{_ISSUER}/.well-known/openid-configuration"
_AUTHORIZE = f"{_ISSUER}/api/accounts/authorize"
_TOKEN = f"{_ISSUER}/api/accounts/oauth/token"
CHATGPT_RESOURCE = "https://api.openai.com/v1"
_DYNAMIC_CLIENT = "dynamic_agent_client"
_SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
_PLAN_SCOPES = {"offline_access", "resource.invoke", "chatgpt.tokens.use.direct"}
_HOST = "127.0.0.1"
_CALLBACK = "/auth/callback"
_ATTEMPT_SECONDS = 300
_EXCHANGE_TIMEOUT = 12


class ChatGPTSignInPending(AuthError):
    """The browser has yet to return the authorization result."""


class _Tokens(BaseModel):
    access_token: str = Field(min_length=1, repr=False)
    refresh_token: str = Field(min_length=1, repr=False)
    id_token: str = Field(min_length=1, repr=False)
    expires_at: float
    refresh_at: float = 0
    scopes: list[str]


class _Account(BaseModel):
    client_id: str
    subject: str = Field(repr=False)
    email: str | None = None
    label: str
    tokens: _Tokens | None = Field(default=None, repr=False)


class _Store(BaseModel):
    host_id: str = ""
    active: str | None = None
    accounts: dict[str, _Account] = Field(default_factory=dict)


@dataclass(frozen=True)
class AccountSummary:
    client_id: str
    label: str
    active: bool
    connected: bool


@dataclass(frozen=True)
class SignIn:
    id: str
    url: str = field(repr=False)


@dataclass
class _Attempt:
    client_id: str | None
    host_id: str
    state: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    nonce: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    verifier: str = field(default_factory=lambda: secrets.token_urlsafe(48), repr=False)
    id: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    server: asyncio.Server | None = None
    expiry: asyncio.TimerHandle | None = None
    redirect: str = ""
    callback: tuple[str, str] | None = field(default=None, repr=False)
    failed: bool = False

    def close(self) -> None:
        if self.server is not None:
            self.server.close()
        if self.expiry is not None:
            self.expiry.cancel()


class ChatGPTAuth:
    def __init__(
        self, path: Path | None = None, *, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.path = path or siwah_home() / "chatgpt" / "auth.json"
        self._transport = transport
        self._attempt: _Attempt | None = None
        self._flow_lock = asyncio.Lock()
        self._refreshes: dict[str, asyncio.Task[str]] = {}

    def _read(self) -> _Store:
        try:
            return _Store.model_validate_json(self.path.read_bytes())
        except FileNotFoundError:
            return _Store()
        except (OSError, ValidationError):
            raise AuthError("Cannot read the local ChatGPT credential store.") from None

    def _write(self, store: _Store) -> None:
        scratch: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=".auth-", suffix=".key", dir=self.path.parent, delete=False
            ) as handle:
                scratch = Path(handle.name)
                handle.write(store.model_dump_json().encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(scratch, self.path)
        except OSError:
            raise AuthError("Cannot update the local ChatGPT credential store.") from None
        finally:
            if scratch is not None:
                with contextlib.suppress(OSError):
                    scratch.unlink(missing_ok=True)

    @contextlib.asynccontextmanager
    async def _locked_store(self) -> AsyncIterator[_Store]:
        try:
            self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            lock = FileLock(self.path.with_suffix(".lock"), mode=0o600)
            deadline = time.monotonic() + 30
            while True:
                try:
                    lock.acquire(timeout=0)
                    break
                except Timeout:
                    if time.monotonic() >= deadline:
                        raise AuthError(
                            "ChatGPT credentials are busy. Try again shortly."
                        ) from None
                    await asyncio.sleep(0.05)
        except OSError:
            raise AuthError("Cannot lock the local ChatGPT credential store.") from None
        try:
            yield self._read()
        finally:
            lock.release()

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=_EXCHANGE_TIMEOUT, transport=self._transport)

    def accounts(self) -> list[AccountSummary]:
        store = self._read()
        return [
            AccountSummary(key, account.label, store.active == key, account.tokens is not None)
            for key, account in store.accounts.items()
        ]

    def connected(self) -> bool:
        return any(account.active and account.connected for account in self.accounts())

    def close(self) -> None:
        if self._attempt is not None:
            self._attempt.close()
            self._attempt = None

    async def begin(self, client_id: str | None = None) -> SignIn:
        async with self._flow_lock:
            async with self._locked_store() as store:
                if client_id is not None and client_id not in store.accounts:
                    raise AuthError("This ChatGPT registration is not saved.")
                if not store.host_id:
                    store.host_id = f"urn:uuid:{uuid4()}"
                    self._write(store)
            self.close()
            attempt = _Attempt(client_id, store.host_id)

            async def receive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                await self._receive(attempt, reader, writer)

            try:
                server = await asyncio.start_server(receive, _HOST, 0, limit=8192)
            except OSError:
                raise AuthError("Could not start the ChatGPT loopback callback.") from None
            attempt.server = server
            port = server.sockets[0].getsockname()[1]
            attempt.redirect = f"http://{_HOST}:{port}{_CALLBACK}"
            attempt.expiry = asyncio.get_running_loop().call_later(_ATTEMPT_SECONDS, self.close)
            self._attempt = attempt
            challenge = base64.urlsafe_b64encode(hashlib.sha256(attempt.verifier.encode()).digest())
            query = {
                "client_id": client_id or _DYNAMIC_CLIENT,
                "ext_agent_host_id": attempt.host_id,
                "response_type": "code",
                "redirect_uri": attempt.redirect,
                "scope": _SCOPE,
                "resource": CHATGPT_RESOURCE,
                "state": attempt.state,
                "nonce": attempt.nonce,
                "code_challenge_method": "S256",
                "code_challenge": challenge.rstrip(b"=").decode(),
            }
            if client_id is None:
                query["agent_name_hint"] = "Siwah"
            # The TUI displays the URL so users can open it manually. Keep any
            # retained ID token out of that URL and let OpenAI select the account.
            return SignIn(attempt.id, f"{_AUTHORIZE}?{urlencode(query)}")

    async def _receive(
        self, attempt: _Attempt, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            async with asyncio.timeout(3):
                raw = await reader.readuntil(b"\r\n\r\n")
                request, headers = raw.split(b"\r\n", 1)
                method, target, _ = request.decode("ascii").split(" ")
                if method != "GET" or not target.startswith("/") or target.startswith("//"):
                    return
                host_headers = [
                    line.partition(b":")[2].strip()
                    for line in headers.split(b"\r\n")
                    if line.partition(b":")[0].lower() == b"host"
                ]
                if host_headers != [urlsplit(attempt.redirect).netloc.encode()]:
                    return
                parsed = urlsplit(target)
                if parsed.path != _CALLBACK:
                    return
                query = parse_qs(parsed.query, max_num_fields=12, keep_blank_values=True)
                accepted = (
                    self._attempt is attempt
                    and attempt.callback is None
                    and not attempt.failed
                    and len(query.get("state", [])) == 1
                    and secrets.compare_digest(query["state"][0].encode(), attempt.state.encode())
                )
                status = "400 Bad Request"
                message = "This ChatGPT callback is invalid. Return to Siwah and try again."
                if accepted:
                    if "error" in query:
                        attempt.failed = True
                        message = "ChatGPT sign-in was declined. Return to Siwah."
                    else:
                        codes = query.get("code", [])
                        clients = query.get("client_id", [attempt.client_id or ""])
                        if (
                            len(codes) == len(clients) == 1
                            and codes[0]
                            and clients[0]
                            and clients[0] != _DYNAMIC_CLIENT
                            and (attempt.client_id is None or clients[0] == attempt.client_id)
                        ):
                            attempt.callback = (codes[0], clients[0])
                            status = "200 OK"
                            message = "ChatGPT authorization received. Return to Siwah to finish."
                writer.write(
                    f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                    f"Cache-Control: no-store\r\nReferrer-Policy: no-referrer\r\n"
                    f"Connection: close\r\nContent-Length: {len(message)}\r\n\r\n{message}".encode()
                )
                await writer.drain()
        except (
            OSError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            ValueError,
        ):
            pass
        finally:
            writer.close()

    async def finish(self, attempt_id: str) -> None:
        async with self._flow_lock:
            attempt = self._attempt
            if attempt is None or not secrets.compare_digest(
                attempt.id.encode(), attempt_id.encode()
            ):
                raise AuthError("This ChatGPT sign-in expired. Start again.")
            if attempt.failed:
                self.close()
                raise AuthError("ChatGPT plan access was declined. Start again to authorize it.")
            if attempt.callback is None:
                raise ChatGPTSignInPending("Finish in your browser first, then try again.")
            code, client_id = attempt.callback
            self.close()
            async with self._http() as http:
                payload = await _exchange(
                    http,
                    {
                        "grant_type": "authorization_code",
                        "client_id": client_id,
                        "code": code,
                        "code_verifier": attempt.verifier,
                        "redirect_uri": attempt.redirect,
                        "resource": CHATGPT_RESOURCE,
                    },
                )
                tokens = _parse_tokens(payload)
                identity = await _verify_identity(http, tokens.id_token, client_id, attempt.nonce)
            async with self._locked_store() as store:
                previous = store.accounts.get(client_id)
                if previous and previous.subject != identity["sub"]:
                    raise AuthError("ChatGPT sign-in returned a different account.")
                email = identity.get("email") if identity.get("email_verified") is True else None
                email = (
                    "".join(char for char in email if char.isprintable())[:120]
                    if isinstance(email, str)
                    else None
                )
                label = previous.label if previous else _label(store, email)
                store.accounts[client_id] = _Account(
                    client_id=client_id,
                    subject=identity["sub"],
                    email=email,
                    label=label,
                    tokens=tokens,
                )
                store.active = client_id
                self._write(store)

    async def access_token(self) -> str:
        store = self._read()
        account = store.accounts.get(store.active or "")
        if account is None or account.tokens is None:
            raise AuthError("Continue with ChatGPT in /connect before using this provider.")
        if _usable(account.tokens):
            return account.tokens.access_token
        task = self._refreshes.get(account.client_id)
        if task is None:
            task = asyncio.create_task(self._refresh(account.client_id))
            self._refreshes[account.client_id] = task

            def finished(result: asyncio.Task[str]) -> None:
                self._refreshes.pop(account.client_id, None)
                if not result.cancelled():
                    result.exception()

            task.add_done_callback(finished)
        # A cancelled caller cannot interrupt rotating refresh-token persistence.
        return await asyncio.shield(task)

    async def _refresh(self, client_id: str) -> str:
        async with self._locked_store() as store:
            account = store.accounts.get(client_id)
            if store.active != client_id or account is None or account.tokens is None:
                raise AuthError("The active ChatGPT account changed. Retry the request.")
            old = account.tokens
            if _usable(old):
                return old.access_token
            async with self._http() as http:
                payload = await _exchange(
                    http,
                    {
                        "grant_type": "refresh_token",
                        "client_id": client_id,
                        "refresh_token": old.refresh_token,
                        "resource": CHATGPT_RESOURCE,
                    },
                )
                tokens = _parse_tokens(payload, old)
                if tokens.id_token != old.id_token:
                    identity = await _verify_identity(http, tokens.id_token, client_id)
                    if account.subject != identity["sub"]:
                        raise AuthError("ChatGPT token renewal returned a different account.")
            account.tokens = tokens
            self._write(store)
            return tokens.access_token

    async def sign_out(self, client_id: str | None = None) -> bool:
        async with self._flow_lock, self._locked_store() as store:
            self.close()
            key = client_id or store.active
            account = store.accounts.get(key or "")
            if account is None or account.tokens is None:
                return True
            async with self._http() as http:
                revoked = await _revoke(http, account.client_id, account.tokens.refresh_token)
            account.tokens = None
            if store.active == key:
                store.active = None
            self._write(store)
            return revoked


def _label(store: _Store, email: str | None) -> str:
    base = email or "ChatGPT account"
    existing = {account.label for account in store.accounts.values()}
    label = base
    number = 2
    while label in existing:
        label = f"{base} ({number})"
        number += 1
    return label


def _usable(tokens: _Tokens) -> bool:
    now = time.time()
    return tokens.expires_at > now and (tokens.expires_at > now + 60 or tokens.refresh_at > now)


async def _exchange(http: httpx.AsyncClient, form: dict[str, str]) -> dict[str, Any]:
    try:
        response = await http.post(_TOKEN, data=form)
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        raise AuthError("Could not reach ChatGPT sign-in. Try again.") from None
    if not isinstance(payload, dict):
        raise AuthError("ChatGPT returned an invalid token response.")
    if response.status_code != 200:
        if payload.get("error") == "invalid_grant":
            raise AuthError(
                "The ChatGPT sign-in has expired or been revoked. Reconnect the account."
            )
        raise AuthError(f"ChatGPT sign-in returned HTTP {response.status_code}. Try again.")
    return payload


def _parse_tokens(payload: dict[str, Any], previous: _Tokens | None = None) -> _Tokens:
    scope = payload.get("scope")
    scopes = scope.split() if isinstance(scope, str) else (previous.scopes if previous else [])
    if not _PLAN_SCOPES.issubset(scopes):
        raise AuthError("The ChatGPT account did not grant plan usage. Reconnect and approve it.")
    expires = payload.get("expires_in")
    kind = payload.get("token_type")
    if (
        type(expires) is not int
        or expires <= 0
        or not isinstance(kind, str)
        or kind.lower() != "bearer"
    ):
        raise AuthError("ChatGPT returned invalid token metadata.")
    earliest = payload.get("earliest_refresh_at")
    if isinstance(earliest, str):
        try:
            earliest = datetime.fromisoformat(earliest.replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError):
            earliest = None
    try:
        return _Tokens.model_validate(
            {
                "access_token": payload.get("access_token"),
                "refresh_token": payload.get("refresh_token"),
                "id_token": payload.get("id_token", previous.id_token if previous else None),
                "expires_at": time.time() + expires,
                "refresh_at": earliest if isinstance(earliest, int | float) else 0,
                "scopes": scopes,
            }
        )
    except (OverflowError, ValidationError):
        raise AuthError("ChatGPT returned incomplete credentials.") from None


async def _metadata(http: httpx.AsyncClient) -> dict[str, Any]:
    response = await http.get(_DISCOVERY)
    response.raise_for_status()
    metadata = response.json()
    if not isinstance(metadata, dict) or metadata.get("issuer") != _ISSUER:
        raise ValueError("Invalid OpenAI identity discovery document")
    return metadata


def _identity_url(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Missing OpenAI identity endpoint")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "auth.openai.com"
        or not parsed.path.startswith("/")
    ):
        raise ValueError("Invalid OpenAI identity endpoint")
    return value


async def _verify_identity(
    http: httpx.AsyncClient, token: str, client_id: str, nonce: str | None = None
) -> dict[str, Any]:
    import jwt

    try:
        metadata = await _metadata(http)
        response = await http.get(_identity_url(metadata.get("jwks_uri")))
        response.raise_for_status()
        header = jwt.get_unverified_header(token)
        keys = jwt.PyJWKSet.from_dict(response.json()).keys
        key = next(
            item
            for item in keys
            if item.key_id == header.get("kid") and item.public_key_use in ("sig", None)
        )
        claims = jwt.decode(
            token,
            key=key.key,
            algorithms=["RS256", "ES256"],
            issuer=_ISSUER,
            audience=client_id,
            leeway=5,
            options={"require": ["sub", "exp", "iat", "iss", "aud"]},
        )
        if not claims["sub"] or (nonce is not None and claims.get("nonce") != nonce):
            raise ValueError("Invalid ChatGPT identity claims")
        audience = claims["aud"]
        azp = claims.get("azp")
        if (isinstance(audience, list) and len(audience) > 1 and azp != client_id) or (
            azp is not None and azp != client_id
        ):
            raise ValueError("Invalid ChatGPT authorized party")
        return claims
    except (StopIteration, KeyError, TypeError, ValueError, httpx.HTTPError, jwt.PyJWTError):
        raise AuthError("Could not validate the ChatGPT account identity. Sign in again.") from None


async def _revoke(http: httpx.AsyncClient, client_id: str, refresh_token: str) -> bool:
    try:
        metadata = await _metadata(http)
        endpoint = _identity_url(metadata.get("revocation_endpoint"))
    except (httpx.HTTPError, ValueError):
        return False
    for attempt in range(2):
        try:
            response = await http.post(
                endpoint,
                data={
                    "token": refresh_token,
                    "token_type_hint": "refresh_token",
                    "client_id": client_id,
                },
            )
            if response.status_code < 500:
                return response.status_code == 200
        except httpx.HTTPError:
            pass
        if not attempt:
            await asyncio.sleep(0.25)
    return False
