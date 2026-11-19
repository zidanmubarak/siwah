from __future__ import annotations

import difflib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from siwah.adapt.level2 import GateCommand, Level2Evolver
from siwah.adapt.models import Candidate


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src" / "siwah" / "tools").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "siwah" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "siwah" / "tools" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "siwah" / "tools" / "edit_file.py").write_text(
        "def apply_edit(text, old, new):\n    return text\n", encoding="utf-8"
    )
    (repo / "tests" / "test_edit_file.py").write_text(
        "from siwah.tools.edit_file import apply_edit\n\n\n"
        "def test_apply_edit():\n"
        "    assert apply_edit('hello world', 'world', 'there') == 'hello there'\n",
        encoding="utf-8",
    )
    (repo / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["src"]\n', encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)
    return repo


def _diff(repo: Path, path: str, old: str, new: str) -> str:
    before = (repo / path).read_text(encoding="utf-8")
    after = before.replace(old, new)
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def _candidate(diff: str, candidate_id: str = "fix-edit-file") -> Candidate:
    return Candidate(
        id=candidate_id,
        level=2,
        kind="core_code",
        scope="global",
        target_paths=["src/siwah/tools/edit_file.py"],
        changes={},
        diff=diff,
        rationale="apply_edit does not apply the replacement",
        expected_metric="pytest passes",
        risk="small core edit",
    )


def _audit(events: list[tuple[str, dict[str, Any]]]):
    def write(event: str, data: dict[str, Any]) -> dict[str, Any]:
        events.append((event, data))
        return {}

    return write


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_level2_fixes_edit_file_and_promotes(tmp_path: Path):
    repo = _repo(tmp_path)
    diff = _diff(
        repo,
        "src/siwah/tools/edit_file.py",
        "return text",
        "return text.replace(old, new, 1)",
    )
    events: list[tuple[str, dict[str, Any]]] = []
    evolver = Level2Evolver(
        repo,
        testing=True,
        commands=[
            GateCommand("pytest", [sys.executable, "-m", "pytest", "-q", "tests"]),
            GateCommand("smoke", [sys.executable, "-c", "import siwah.tools.edit_file"]),
        ],
        post_check=lambda _repo: True,
        restart=lambda: True,
        audit=_audit(events),
    )

    result = evolver.run(_candidate(diff))

    assert result.status == "promoted"
    assert result.promotion is not None
    assert result.promotion.tag == "evo-core-1"
    assert "text.replace" in (repo / "src" / "siwah" / "tools" / "edit_file.py").read_text(
        encoding="utf-8"
    )
    assert any(event == "evolve.level2.promoted" for event, _ in events)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_level2_rejects_a_patch_that_weakens_tests(tmp_path: Path):
    repo = _repo(tmp_path)
    diff = _diff(
        repo,
        "tests/test_edit_file.py",
        "== 'hello there'",
        "== 'hello world'",
    )
    evolver = Level2Evolver(repo)

    result = evolver.run(_candidate(diff, "weaken-test"))

    assert result.status == "rejected"
    assert "may not weaken existing tests" in (result.reason or "")
    assert (
        subprocess.run(
            ["git", "tag", "--list", "evo-core-*"], cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        == ""
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_level2_rolls_back_a_failed_post_promotion_boot(tmp_path: Path):
    repo = _repo(tmp_path)
    diff = _diff(
        repo,
        "src/siwah/tools/edit_file.py",
        "return text",
        "return text.replace(old, new, 1)",
    )
    events: list[tuple[str, dict[str, Any]]] = []
    evolver = Level2Evolver(
        repo,
        testing=True,
        commands=[GateCommand("smoke", [sys.executable, "-c", "import siwah.tools.edit_file"])],
        post_check=lambda _repo: False,
        restart=lambda: True,
        audit=_audit(events),
    )

    result = evolver.run(_candidate(diff, "boot-break"))

    assert result.status == "rolled_back"
    assert "return text\n" in (repo / "src" / "siwah" / "tools" / "edit_file.py").read_text(
        encoding="utf-8"
    )
    assert (
        subprocess.run(
            ["git", "tag", "--list", "evo-core-*"], cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        == ""
    )
    assert any(event == "evolve.level2.rollback" for event, _ in events)
