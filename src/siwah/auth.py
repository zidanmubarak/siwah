"""Provider credential resolution.

Keys never enter logs, traces, sessions, or error messages. The environment is
preferred; `~/.siwah/auth.json` is the fallback and is written with mode 600.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import AuthConfig
from .paths import auth_path, write_private


class AuthError(RuntimeError):
    """A required credential is missing or unreadable."""


class AuthStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or auth_path()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"providers": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AuthError(f"Cannot read auth file {self.path}: {exc}") from exc
        if not isinstance(data, dict):
            raise AuthError(f"Auth file {self.path} must contain a JSON object")
        providers = data.get("providers")
        if not isinstance(providers, dict):
            data = {"providers": {}}
        return data

    def get_key(self, provider: str, auth: AuthConfig) -> str | None:
        if auth.scheme == "none":
            return None
        if auth.env:
            value = os.environ.get(auth.env)
            if value:
                return value
        data = self._read()
        entry = data.get("providers", {}).get(provider)
        if isinstance(entry, dict):
            key = entry.get("api_key")
            if isinstance(key, str) and key:
                return key
        return None

    def require_key(self, provider: str, auth: AuthConfig) -> str:
        key = self.get_key(provider, auth)
        if not key:
            hint = f" Set {auth.env}." if auth.env else ""
            raise AuthError(
                f"No credential for provider '{provider}'. Run `siwah auth login {provider}` "
                f"or set the configured environment variable.{hint}"
            )
        return key

    def save_key(self, provider: str, key: str) -> Path:
        data = self._read()
        providers = data.setdefault("providers", {})
        providers[provider] = {"api_key": key}
        write_private(self.path, json.dumps(data, indent=2) + "\n")
        return self.path

    def delete_key(self, provider: str) -> bool:
        data = self._read()
        providers = data.get("providers", {})
        if provider not in providers:
            return False
        del providers[provider]
        write_private(self.path, json.dumps(data, indent=2) + "\n")
        return True

    def configured(self, provider: str) -> bool:
        entry = self._read().get("providers", {}).get(provider)
        return isinstance(entry, dict) and bool(entry.get("api_key"))
