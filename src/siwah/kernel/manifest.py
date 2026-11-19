"""Deterministic SHA-256 manifest for kernel and eval files."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .boundary import PROTECTED_SOURCE_PATHS
from .paths import write_private

MANIFEST_NAME = "kernel.manifest.json"
MANIFEST_VERSION = 1
_IGNORED_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
_IGNORED_SUFFIXES = {".pyc", ".pyo"}


@dataclass(frozen=True)
class ManifestStatus:
    present: bool
    ok: bool
    expected_hash: str | None = None
    actual_hash: str | None = None
    changed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    error: str | None = None

    def summary(self) -> str:
        if not self.present:
            return "manifest missing"
        if self.ok:
            return f"manifest ok ({self.actual_hash[:12] if self.actual_hash else '?'})"
        parts = []
        if self.expected_hash and self.actual_hash:
            parts.append(f"{self.expected_hash[:12]} != {self.actual_hash[:12]}")
        if self.changed:
            parts.append(f"changed={','.join(self.changed)}")
        if self.missing:
            parts.append(f"missing={','.join(self.missing)}")
        if self.extra:
            parts.append(f"extra={','.join(self.extra)}")
        if self.error:
            parts.append(self.error)
        return "manifest mismatch: " + "; ".join(parts)


def manifest_path(repo: Path | str) -> Path:
    return Path(repo).resolve() / MANIFEST_NAME


def _iter_tree(repo: Path, relative_root: str) -> list[Path]:
    root = repo / relative_root
    if root.is_file():
        return [root]
    if not root.is_dir():
        return []
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _IGNORED_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in _IGNORED_SUFFIXES:
            continue
        files.append(path)
    return files


def _canonical_bytes(path: Path) -> bytes:
    """Hash text consistently on Windows (CRLF) and Linux/macOS (LF)."""

    return path.read_bytes().replace(b"\r\n", b"\n")


def file_hashes(repo: Path | str) -> dict[str, str]:
    """Return POSIX relative path -> SHA-256 for the protected trees.

    Git may check out the same blob with CRLF on Windows and LF elsewhere. The
    manifest must describe the committed content, not the checkout's line
    endings, so hashing normalizes CRLF to LF.
    """

    root = Path(repo).resolve()
    hashes: dict[str, str] = {}
    for relative_root in PROTECTED_SOURCE_PATHS:
        for path in _iter_tree(root, relative_root):
            relative = path.relative_to(root).as_posix()
            hashes[relative] = hashlib.sha256(_canonical_bytes(path)).hexdigest()
    return dict(sorted(hashes.items()))


def tree_hash(files: dict[str, str]) -> str:
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_manifest(repo: Path | str) -> dict[str, Any]:
    files = file_hashes(repo)
    return {
        "version": MANIFEST_VERSION,
        "hash": tree_hash(files),
        "files": files,
    }


def write_manifest(repo: Path | str, path: Path | str | None = None) -> tuple[Path, dict[str, Any]]:
    manifest = build_manifest(repo)
    target = Path(path) if path is not None else manifest_path(repo)
    write_private(target, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return target, manifest


def load_manifest(repo: Path | str, path: Path | str | None = None) -> dict[str, Any] | None:
    target = Path(path) if path is not None else manifest_path(repo)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def verify_manifest(repo: Path | str, path: Path | str | None = None) -> ManifestStatus:
    target = Path(path) if path is not None else manifest_path(repo)
    manifest = load_manifest(repo, target)
    actual = build_manifest(repo)
    if manifest is None:
        return ManifestStatus(
            present=target.exists(),
            ok=False,
            actual_hash=actual["hash"],
            error="manifest cannot be read" if target.exists() else None,
        )
    expected_files_raw = manifest.get("files")
    expected_files = expected_files_raw if isinstance(expected_files_raw, dict) else {}
    expected = {
        str(name): str(digest)
        for name, digest in expected_files.items()
        if isinstance(name, str) and isinstance(digest, str)
    }
    actual_files: dict[str, str] = actual["files"]
    changed = sorted(
        name
        for name in expected.keys() & actual_files.keys()
        if expected[name] != actual_files[name]
    )
    missing = sorted(expected.keys() - actual_files.keys())
    extra = sorted(actual_files.keys() - expected.keys())
    expected_hash = manifest.get("hash")
    if not isinstance(expected_hash, str):
        expected_hash = tree_hash(expected)
    expected_hash_valid = tree_hash(expected) == expected_hash
    ok = (
        expected_hash_valid
        and expected_hash == actual["hash"]
        and not changed
        and not missing
        and not extra
    )
    error = None if expected_hash_valid else "manifest tree hash is invalid"
    return ManifestStatus(
        present=True,
        ok=ok,
        expected_hash=expected_hash,
        actual_hash=actual["hash"],
        changed=changed,
        missing=missing,
        extra=extra,
        error=error,
    )


__all__ = [
    "MANIFEST_NAME",
    "MANIFEST_VERSION",
    "ManifestStatus",
    "build_manifest",
    "file_hashes",
    "load_manifest",
    "manifest_path",
    "tree_hash",
    "verify_manifest",
    "write_manifest",
]
