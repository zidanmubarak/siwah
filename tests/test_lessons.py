from __future__ import annotations

from pathlib import Path

from siwah.harness.bootstrap import ensure_harness
from siwah.harness.lessons import (
    format_lessons,
    lessons_context,
    read_lessons,
    search_lessons,
    write_lesson,
)
from siwah.harness.prompt import build_system_prompt


def test_lessons_are_private_searchable_and_loaded(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    ensure_harness()
    workspace = tmp_path / "repo"
    workspace.mkdir()

    path, changed = write_lesson(
        workspace,
        target="user",
        cue="Debugging flaky tests",
        lesson="Run the failing test three times before changing production code.",
        evidence="pytest -q tests/test_flaky.py",
        confidence="high",
    )
    assert changed is True
    assert path.is_file()
    assert not write_lesson(
        workspace,
        target="user",
        cue="Debugging flaky tests",
        lesson="Run the failing test three times before changing production code.",
    )[1]
    lessons = read_lessons(workspace)
    assert len(lessons) == 1
    assert lessons[0].confidence == "high"
    assert "three times" in format_lessons(search_lessons(workspace, "flaky test"))
    assert "Debugging flaky tests" in lessons_context(workspace)
    assert "Debugging flaky tests" in build_system_prompt(workspace)
