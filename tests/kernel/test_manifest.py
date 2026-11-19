from __future__ import annotations

import json
from pathlib import Path

from siwah.kernel.manifest import (
    build_manifest,
    manifest_path,
    verify_manifest,
    write_manifest,
)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src" / "siwah" / "kernel").mkdir(parents=True)
    (repo / "evals" / "golden").mkdir(parents=True)
    (repo / "src" / "siwah" / "kernel" / "guard.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "evals" / "golden" / "task.json").write_text("{}\n", encoding="utf-8")
    return repo


def test_manifest_round_trip_and_ignores_cache(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / "src" / "siwah" / "kernel" / "__pycache__").mkdir()
    (repo / "src" / "siwah" / "kernel" / "__pycache__" / "guard.pyc").write_bytes(b"cache")

    path, manifest = write_manifest(repo)

    assert path == manifest_path(repo)
    assert manifest["version"] == 1
    assert sorted(manifest["files"]) == [
        "evals/golden/task.json",
        "src/siwah/kernel/guard.py",
    ]
    status = verify_manifest(repo)
    assert status.present and status.ok and not status.changed


def test_manifest_detects_changed_missing_and_extra_files(tmp_path: Path):
    repo = _repo(tmp_path)
    write_manifest(repo)
    kernel_file = repo / "src" / "siwah" / "kernel" / "guard.py"

    kernel_file.write_text("x = 2\n", encoding="utf-8")
    changed = verify_manifest(repo)
    assert not changed.ok
    assert changed.changed == ["src/siwah/kernel/guard.py"]

    (repo / "evals" / "golden" / "task.json").unlink()
    missing = verify_manifest(repo)
    assert "evals/golden/task.json" in missing.missing

    (repo / "evals" / "new.txt").write_text("extra\n", encoding="utf-8")
    extra = verify_manifest(repo)
    assert "evals/new.txt" in extra.extra


def test_manifest_rejects_corrupt_tree_hash(tmp_path: Path):
    repo = _repo(tmp_path)
    path, manifest = write_manifest(repo)
    manifest["hash"] = "0" * 64
    path.write_text(json.dumps(manifest), encoding="utf-8")

    status = verify_manifest(repo)
    assert not status.ok
    assert status.error == "manifest tree hash is invalid"


def test_missing_manifest_is_reported(tmp_path: Path):
    repo = _repo(tmp_path)
    status = verify_manifest(repo)
    assert not status.present
    assert not status.ok
    assert status.summary() == "manifest missing"


def test_manifest_is_deterministic(tmp_path: Path):
    repo = _repo(tmp_path)
    first = build_manifest(repo)
    second = build_manifest(repo)
    assert first == second


def test_manifest_is_line_ending_stable(tmp_path: Path):
    repo = _repo(tmp_path)
    path = repo / "src" / "siwah" / "kernel" / "guard.py"
    path.write_bytes(b"x = 1\r\ny = 2\r\n")
    crlf = build_manifest(repo)

    path.write_bytes(b"x = 1\ny = 2\n")
    assert build_manifest(repo) == crlf
