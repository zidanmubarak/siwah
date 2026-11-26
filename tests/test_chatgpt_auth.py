from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import stat
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from siwah.auth import AuthError
from siwah.providers.chatgpt_auth import ChatGPTAuth, ChatGPTSignInPending, SignIn

_AUTH = "https://auth.openai.com"
_SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"


class Issuer:
    def __init__(self):
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_key = RSAAlgorithm.to_jwk(self.private_key.public_key(), as_dict=True)
        self.public_key.update(kid="signing-key", use="sig", alg="RS256")
        self.params: dict[str, str] = {}
        self.sub = "account"
        self.next_scope = _SCOPES
        self.refreshes = 0
        self.revoke_status = 200
        self.revoked: list[str] = []
        self.refresh_error: str | None = None
        self.omit_refresh = False

    def payload(self, **overrides: str | int) -> dict[str, str | int]:
        return {
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "scope": self.next_scope,
            "expires_in": 3600,
            "token_type": "Bearer",
            **overrides,
        }

    def id_token(self, client: str) -> str:
        return jwt.encode(
            {
                "sub": self.sub,
                "email": "dev@example.com",
                "email_verified": True,
                "aud": client,
                "iss": _AUTH,
                "iat": int(time.time()),
                "exp": int(time.time()) + 300,
                "nonce": self.params["nonce"],
            },
            self.private_key,
            algorithm="RS256",
            headers={"kid": "signing-key"},
        )

    async def respond(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "auth.openai.com"
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": _AUTH,
                    "jwks_uri": f"{_AUTH}/jwks",
                    "revocation_endpoint": f"{_AUTH}/revoke",
                },
            )
        if request.url.path == "/jwks":
            return httpx.Response(200, json={"keys": [self.public_key]})
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        if request.url.path == "/revoke":
            assert form["token_type_hint"] == "refresh_token"
            self.revoked.append(form["token"])
            return httpx.Response(self.revoke_status)
        assert request.url.path == "/api/accounts/oauth/token"
        assert "client_secret" not in form
        assert form["resource"] == "https://api.openai.com/v1"
        if form["grant_type"] == "refresh_token":
            self.refreshes += 1
            assert form["refresh_token"] == "refresh-secret"
            assert "scope" not in form
            await asyncio.sleep(0.06)
            if self.refresh_error is not None:
                return httpx.Response(400, json={"error": self.refresh_error})
            payload = self.payload(access_token="rotated-access", refresh_token="rotated-refresh")
            if self.omit_refresh:
                payload.pop("refresh_token")
            return httpx.Response(200, json=payload)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(form["code_verifier"].encode()).digest()
        )
        assert self.params["code_challenge"] == challenge.rstrip(b"=").decode()
        assert self.params["redirect_uri"] == form["redirect_uri"]
        assert form["code"] == "fixture-code"
        return httpx.Response(200, json=self.payload(id_token=self.id_token(form["client_id"])))

    def auth(self, path: Path) -> ChatGPTAuth:
        return ChatGPTAuth(path, transport=httpx.MockTransport(self.respond))

    def query(self, attempt: SignIn) -> dict[str, str]:
        url = urlsplit(attempt.url)
        assert (url.scheme, url.netloc, url.path) == (
            "https",
            "auth.openai.com",
            "/api/accounts/authorize",
        )
        self.params = {key: values[0] for key, values in parse_qs(url.query).items()}
        return self.params

    async def callback(self, **overrides: str) -> httpx.Response:
        params = {"state": self.params["state"], "code": "fixture-code", **overrides}
        async with httpx.AsyncClient(trust_env=False) as client:
            return await client.get(self.params["redirect_uri"], params=params)


@pytest.mark.asyncio
async def test_chatgpt_sign_in_binds_loopback_and_switches_verified_registration(tmp_path):
    path = tmp_path / "auth.json"
    issuer = Issuer()
    auth = issuer.auth(path)
    attempt = await auth.begin()
    first = issuer.query(attempt)
    assert first["client_id"] == "dynamic_agent_client"
    assert first["agent_name_hint"] == "Siwah"
    assert first["code_challenge_method"] == "S256"
    assert first["ext_agent_host_id"].startswith("urn:uuid:")
    assert first["redirect_uri"].startswith("http://127.0.0.1:")
    assert not auth.connected()
    with pytest.raises(ChatGPTSignInPending):
        await auth.finish(attempt.id)
    assert (await issuer.callback(state="wrong", client_id="oaiapp_first")).status_code == 400
    assert (await issuer.callback(client_id="oaiapp_first")).status_code == 200
    assert (await issuer.callback(client_id="oaiapp_bad")).status_code == 400
    await auth.finish(attempt.id)
    with pytest.raises(AuthError, match="expired"):
        await auth.finish(attempt.id)
    assert auth.connected()
    assert await auth.access_token() == "access-secret"
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    restarted = issuer.auth(path)
    issuer.sub = "another"
    attempt = await restarted.begin()
    again = issuer.query(attempt)
    assert again["ext_agent_host_id"] == first["ext_agent_host_id"]
    assert again["nonce"] != first["nonce"] and again["state"] != first["state"]
    assert (await issuer.callback(client_id="oaiapp_second")).status_code == 200
    await restarted.finish(attempt.id)
    labels = [(account.label, account.active) for account in restarted.accounts()]
    assert labels == [("dev@example.com", False), ("dev@example.com (2)", True)]
    attempt = await restarted.begin("oaiapp_first")
    reuse = issuer.query(attempt)
    assert reuse["client_id"] == "oaiapp_first"
    assert "agent_name_hint" not in reuse and "id_token_hint" not in reuse
    assert (await issuer.callback(client_id="oaiapp_second")).status_code == 400
    assert (await issuer.callback()).status_code == 200
    with pytest.raises(AuthError, match="different account"):
        await restarted.finish(attempt.id)
    assert restarted.accounts()[1].active


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["denied", "scope", "nonce"])
async def test_chatgpt_does_not_write_credentials_without_valid_consent(tmp_path, reason):
    issuer = Issuer()
    path = tmp_path / "auth.json"
    auth = issuer.auth(path)
    attempt = await auth.begin()
    issuer.query(attempt)
    if reason == "denied":
        assert (await issuer.callback(error="access_denied")).status_code == 400
        expected = "declined"
    else:
        if reason == "scope":
            issuer.next_scope = "openid profile email"
        assert (await issuer.callback(client_id="oaiapp_fixture")).status_code == 200
        if reason == "nonce":
            issuer.params["nonce"] = "another-nonce"
        expected = "plan usage" if reason == "scope" else "identity"
    with pytest.raises(AuthError, match=expected):
        await auth.finish(attempt.id)
    assert not auth.connected()
    assert not auth.accounts()
    assert "refresh-secret" not in path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_chatgpt_refresh_serializes_rotating_credentials_and_revoke_clears_tokens(tmp_path):
    issuer = Issuer()
    path = tmp_path / "auth.json"
    first, other = issuer.auth(path), issuer.auth(path)
    attempt = await first.begin()
    issuer.query(attempt)
    assert (await issuer.callback(client_id="oaiapp_restore")).status_code == 200
    await first.finish(attempt.id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["accounts"]["oaiapp_restore"]["tokens"]["expires_at"] = time.time() - 1
    path.write_text(json.dumps(data), encoding="utf-8")
    results = await asyncio.gather(first.access_token(), first.access_token(), other.access_token())
    assert results == ["rotated-access"] * 3
    assert issuer.refreshes == 1
    assert await other.sign_out()
    assert issuer.revoked == ["rotated-refresh"]
    saved = first.accounts()
    assert len(saved) == 1 and not saved[0].active and not saved[0].connected
    for token in ["rotated-access", "rotated-refresh", "refresh-secret", "access-secret"]:
        assert token not in path.read_text(encoding="utf-8")
    with pytest.raises(AuthError, match="Continue with ChatGPT"):
        await first.access_token()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "invalid_grant",
        "invalid_refresh_token",
        "token_expired",
        "refresh_token_expired",
        "refresh_token_invalidated",
        "refresh_token_reused",
        "invalid_client",
    ],
)
async def test_rejected_refresh_token_clears_the_account(tmp_path, code):
    issuer = Issuer()
    issuer.refresh_error = code
    auth = issuer.auth(tmp_path / "auth.json")
    attempt = await auth.begin()
    issuer.query(attempt)
    assert (await issuer.callback(client_id="oaiapp_refresh")).status_code == 200
    await auth.finish(attempt.id)
    data = json.loads(auth.path.read_text(encoding="utf-8"))
    data["accounts"]["oaiapp_refresh"]["tokens"]["expires_at"] = time.time() - 1
    auth.path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(AuthError, match="expired or been revoked"):
        await auth.access_token()
    assert issuer.refreshes == 1
    [account] = auth.accounts()
    assert account.connected is False
    with pytest.raises(AuthError, match="Continue with ChatGPT"):
        await auth.access_token()
    assert issuer.refreshes == 1


@pytest.mark.asyncio
async def test_refresh_keeps_the_previous_refresh_token_when_omitted(tmp_path):
    issuer = Issuer()
    issuer.omit_refresh = True
    auth = issuer.auth(tmp_path / "auth.json")
    attempt = await auth.begin()
    issuer.query(attempt)
    assert (await issuer.callback(client_id="oaiapp_refresh")).status_code == 200
    await auth.finish(attempt.id)
    data = json.loads(auth.path.read_text(encoding="utf-8"))
    data["accounts"]["oaiapp_refresh"]["tokens"]["expires_at"] = time.time() - 1
    auth.path.write_text(json.dumps(data), encoding="utf-8")

    assert await auth.access_token() == "rotated-access"
    assert await auth.sign_out()
    assert issuer.revoked == ["refresh-secret"]


def test_credential_write_retries_a_shared_replace(tmp_path, monkeypatch):
    from siwah.providers.chatgpt_auth import _Store

    auth = ChatGPTAuth(tmp_path / "auth.json")
    real_replace = os.replace
    calls = 0

    def flaky_replace(source, target):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise PermissionError("file is in use")
        real_replace(source, target)

    monkeypatch.setattr("siwah.providers.chatgpt_auth.os.replace", flaky_replace)
    auth._write(_Store())
    assert calls == 3
    assert auth._read().model_dump() == _Store().model_dump()
