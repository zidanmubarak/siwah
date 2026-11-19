from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from siwah.adapt.best_of_n import BestOfN


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "file.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)
    return repo


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
@pytest.mark.asyncio
async def test_best_of_n_applies_the_first_passing_worktree(tmp_path: Path):
    repo = _repo(tmp_path)

    async def runner(path: Path, index: int) -> bool:
        (path / "file.txt").write_text(f"attempt {index}\n", encoding="utf-8")
        return index == 1

    result = await BestOfN(repo, runner=runner, attempts=3).run()

    assert result.status == "promoted"
    assert result.winner == 1
    assert (repo / "file.txt").read_text(encoding="utf-8") == "attempt 1\n"
    assert len(result.attempts) == 3
    worktrees = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert worktrees.count("worktree ") == 1


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
@pytest.mark.asyncio
async def test_best_of_n_leaves_the_repo_alone_when_no_attempt_passes(tmp_path: Path):
    repo = _repo(tmp_path)

    async def runner(path: Path, index: int) -> bool:
        (path / "file.txt").write_text(f"attempt {index}\n", encoding="utf-8")
        return False

    result = await BestOfN(repo, runner=runner, attempts=2).run()

    assert result.status == "rejected"
    assert (repo / "file.txt").read_text(encoding="utf-8") == "base\n"
