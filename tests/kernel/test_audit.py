from __future__ import annotations

from pathlib import Path

from siwah.kernel.audit import audit_event, audit_path, read_events, verify_chain


def test_audit_appends_hash_chain_and_redacts(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    first = audit_event("boot.ok", {"step": 1})
    second = audit_event("boot.failed", {"error": "sk-abcdefghijklmnopqrstuvwx"})

    assert first["prev"] == ""
    assert second["prev"] == first["hash"]
    events = read_events(limit=10)
    assert [event["event"] for event in events] == ["boot.ok", "boot.failed"]
    assert "sk-abcdefghijklmnopqrstuvwx" not in audit_path().read_text(encoding="utf-8")
    assert verify_chain()


def test_audit_detects_tampering(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    audit_event("one", {"value": 1})
    path = audit_path()
    path.write_text(path.read_text(encoding="utf-8").replace('"one"', '"two"'), encoding="utf-8")
    assert verify_chain() is False


def test_audit_limit_and_missing_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    assert read_events(limit=5) == []
    for index in range(5):
        audit_event("event", {"index": index})
    events = read_events(limit=2)
    assert [event["data"]["index"] for event in events] == [3, 4]
