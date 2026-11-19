"""Trusted secret detection shared by admission, persistence and audit."""

from __future__ import annotations

import re

_SECRET_PATTERNS = [
    re.compile(
        r"-----BEGIN (?P<key_type>(?:RSA |OPENSSH |EC |DSA |ENCRYPTED )?PRIVATE KEY)-----"
        r".*?(?:-----END (?P=key_type)-----|\Z)",
        re.DOTALL,
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|secret|password|token|access_token|refresh_token|client_secret)"
        r"['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{16,}"
    ),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"),
]


def secret_marker(value: str) -> str | None:
    """Return a generic marker, never the matched credential."""
    return next(
        ("credential-like value" for pattern in _SECRET_PATTERNS if pattern.search(value)), None
    )


def redact_secrets(value: str) -> str:
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    return value


__all__ = ["redact_secrets", "secret_marker"]
