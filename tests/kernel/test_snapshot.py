from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from siwah.kernel.manifest import verify_manifest, write_manifest
from siwah.kernel.snapshot import (
    create_healthy_snapshot,
    healthy_tag,
    latest_healthy_tag,
    rollback_to_tag,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_snapshot_and_rollback_restore_only_protected_paths(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "src" / "siwah" / "kernel").mkdir(parents=True)
    (repo / "evals" / "golden").mkdir(parents=True)
    (repo / "src" / "siwah" / "kernel" / "guard.py").write_text("original\n", encoding="utf-8")
    (repo / "evals" / "golden" / "task.json").write_text("{}\n", encoding="utf-8")
    (repo / "user.txt").write_text("user\n", encoding="utf-8")
    write_manifest(repo)
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")

    status = verify_manifest(repo)
    assert status.actual_hash is not None
    tag = create_healthy_snapshot(repo, status.actual_hash)
    assert tag is not None
    assert latest_healthy_tag(repo) == tag

    (repo / "src" / "siwah" / "kernel" / "guard.py").write_text("broken\n", encoding="utf-8")
    (repo / "evals" / "golden" / "extra.json").write_text("{}\n", encoding="utf-8")
    _git(repo, "add", "evals/golden/extra.json")
    _git(repo, "commit", "-m", "candidate")
    (repo / "user.txt").write_text("keep me\n", encoding="utf-8")

    result = rollback_to_tag(repo, tag)

    assert result.ok
    assert (repo / "src" / "siwah" / "kernel" / "guard.py").read_text(
        encoding="utf-8"
    ) == "original\n"
    assert not (repo / "evals" / "golden" / "extra.json").exists()
    assert (repo / "user.txt").read_text(encoding="utf-8") == "keep me\n"
    assert verify_manifest(repo).ok


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_dirty_protected_tree_does_not_get_a_snapshot(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "evals").mkdir(parents=True)
    (repo / "evals" / "task.json").write_text("{}\n", encoding="utf-8")
    write_manifest(repo)
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    status = verify_manifest(repo)
    assert status.actual_hash is not None
    (repo / "evals" / "task.json").write_text("dirty\n", encoding="utf-8")
    assert create_healthy_snapshot(repo, status.actual_hash) is None


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_unsigned_healthy_tag_is_rejected(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "src" / "siwah" / "kernel").mkdir(parents=True)
    (repo / "evals").mkdir(parents=True)
    (repo / "src" / "siwah" / "kernel" / "guard.py").write_text("original\n", encoding="utf-8")
    (repo / "evals" / "task.json").write_text("{}\n", encoding="utf-8")
    write_manifest(repo)
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    digest = verify_manifest(repo).actual_hash
    assert digest
    forged = healthy_tag(digest)
    _git(repo, "tag", forged)

    assert latest_healthy_tag(repo) is None
    result = rollback_to_tag(repo, forged)
    assert not result.ok
    assert "verified healthy snapshot" in result.detail
