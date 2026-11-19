from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_siwah_home(tmp_path, monkeypatch):
    """Keep supervisor signing keys and audit logs out of the real user home."""

    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
