from __future__ import annotations

from pathlib import Path

from siwah.harness.bootstrap import ensure_harness
from siwah.harness.profile import profile_context, read_profile, update_profile


def test_profile_updates_context_and_rejects_secrets(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    ensure_harness()
    data = update_profile(
        language="Indonesian",
        communication="Concise, direct",
        stack="Python, TypeScript",
        testing="pytest, bun test",
        risk_tolerance="Use judgment; ask before destructive actions",
        preferences="Prefer small commits",
    )
    assert data["language"] == "Indonesian"
    assert data["stack"] == ["Python", "TypeScript"]
    context = profile_context()
    assert "Indonesian" in context
    assert "Python, TypeScript" in context
    assert read_profile()["testing"] == ["pytest", "bun test"]
    try:
        update_profile(notes="token=" + "a" * 30)
    except ValueError as exc:
        assert "credential" in str(exc)
    else:
        raise AssertionError("secret profile should be rejected")
