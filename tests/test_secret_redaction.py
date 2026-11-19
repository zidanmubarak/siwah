from __future__ import annotations

import pytest

from siwah.harness.memory import redact_secrets, secret_marker


@pytest.mark.parametrize(
    "text",
    [
        '{"password": "synthetic-password-12345"}',
        '{"api_key":"synthetic-password-12345"}',
        "'token': 'synthetic-password-12345'",
        "password=synthetic-password-12345",
        "-----BEGIN PRIVATE KEY-----\nsynthetic-password-12345\n-----END PRIVATE KEY-----",
        "-----BEGIN RSA PRIVATE KEY-----\nsynthetic-password-12345\n-----END RSA PRIVATE KEY-----",
        "-----BEGIN OPENSSH PRIVATE KEY-----\nsynthetic-password-12345",
    ],
)
def test_redaction_removes_quoted_credentials_and_complete_private_keys(text):
    assert secret_marker(text)
    redacted = redact_secrets(text)
    assert "synthetic-password-12345" not in redacted
    assert "[REDACTED]" in redacted


def test_private_key_redaction_preserves_surrounding_non_secret_text():
    text = "Before\n-----BEGIN PRIVATE KEY-----\nkey-body\n-----END PRIVATE KEY-----\nAfter"
    assert redact_secrets(text) == "Before\n[REDACTED]\nAfter"
