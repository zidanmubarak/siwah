"""OpenRouter browser authorization for local and remote terminals."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

from ..auth import AuthError
from ..config import ProviderConfig

BASE_URL = "https://openrouter.ai/api/v1"
AUTH_URL = "https://openrouter.ai/auth"


def supports_browser_login(config: ProviderConfig) -> bool:
    # Never send an account-issued credential to an arbitrary custom endpoint.
    return (
        config.auth.scheme == "bearer"
        and config.auth.browser == "openrouter"
        and config.base_url == BASE_URL
        and config.models_url in {None, f"{BASE_URL}/models"}
    )


@dataclass(frozen=True)
class OpenRouterLogin:
    verifier: str = field(default_factory=lambda: secrets.token_urlsafe(48), repr=False)
    expires_at: float = field(default_factory=lambda: time.monotonic() + 600)

    def authorization_url(self) -> str:
        challenge = hashlib.sha256(self.verifier.encode("ascii")).digest()
        query = urlencode(
            {
                "code_challenge": base64.urlsafe_b64encode(challenge).decode("ascii").rstrip("="),
                "code_challenge_method": "S256",
                "key_label": "Siwah",
            }
        )
        return f"{AUTH_URL}?{query}"

    async def exchange(
        self, code: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> str:
        if time.monotonic() >= self.expires_at:
            raise AuthError("Sign-in expired. Start a new browser sign-in.")
        if not code.strip():
            raise AuthError("Enter the sign-in code from OpenRouter.")
        try:
            async with httpx.AsyncClient(timeout=15, transport=transport) as client:
                response = await client.post(
                    f"{BASE_URL}/auth/keys",
                    json={
                        "code": code.strip(),
                        "code_verifier": self.verifier,
                        "code_challenge_method": "S256",
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise AuthError("OpenRouter could not verify the code. Start a new sign-in.") from None
        key = payload.get("key") if isinstance(payload, dict) else None
        if not isinstance(key, str) or not key.strip():
            raise AuthError("OpenRouter did not return a credential.")
        return key
