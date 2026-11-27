from __future__ import annotations

from pathlib import Path

from siwah.harness.bootstrap import ensure_harness
from siwah.harness.memory import write_memory
from siwah.harness.memory_review import dedupe_memory, review_memory


def test_memory_review_detects_duplicates_and_conflicts(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    ensure_harness()
    workspace = tmp_path / "repo"
    workspace.mkdir()
    write_memory(workspace, target="user", content="test command: pytest -q")
    write_memory(workspace, target="project", content="test command: pytest -q")
    write_memory(workspace, target="user", content="test command: npm test")
    findings = review_memory(workspace)
    assert any(item.kind == "duplicate" for item in findings)
    assert any(item.kind == "conflict" for item in findings)

    result = dedupe_memory(workspace, apply=True)
    assert result["removed"]
    assert not any(item.kind == "duplicate" for item in review_memory(workspace))
