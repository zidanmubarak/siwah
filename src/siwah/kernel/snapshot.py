"""Snapshot and rollback of protected paths through git tags."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .boundary import PROTECTED_RECOVERY_PATHS
from .manifest import MANIFEST_NAME, build_manifest, tree_hash, verify_manifest
from .paths import siwah_home, write_private

HEALTHY_PREFIX = "kernel-healthy-"
PROTECTED_ROLLBACK_PATHS = PROTECTED_RECOVERY_PATHS
SIGNATURE_PREFIX = "siwah-hmac:"
SUPERVISOR_KEY_ENV = "SIWAH_SUPERVISOR_KEY"


def _supervisor_key_path() -> Path:
    override = os.environ.get(SUPERVISOR_KEY_ENV)
    if override:
        return Path(override).expanduser()
    return siwah_home() / "supervisor.key"


def _load_supervisor_key(*, create: bool) -> bytes | None:
    path = _supervisor_key_path()
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        if not create:
            return None
        raw = secrets.token_hex(32)
        try:
            write_private(path, raw + "\n")
        except OSError:
            return None
    except OSError:
        return None
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        return None
    return key if len(key) >= 16 else None


def _signature_payload(tag: str, manifest_hash: str, commit: str) -> bytes:
    return f"siwah-healthy\n{tag}\n{manifest_hash}\n{commit}\n".encode()


def _sign_tag(tag: str, manifest_hash: str, commit: str) -> str | None:
    key = _load_supervisor_key(create=True)
    if key is None:
        return None
    digest = hmac.new(key, _signature_payload(tag, manifest_hash, commit), hashlib.sha256)
    return f"{SIGNATURE_PREFIX}{digest.hexdigest()}"


def _tag_signature_valid(repo: Path, tag: str, manifest_hash: str) -> bool:
    """Verify the HMAC annotation written by create_healthy_snapshot."""

    commit = _git(repo, "rev-parse", f"{tag}^{{commit}}")
    if commit.returncode != 0:
        return False
    annotation = _git(repo, "for-each-ref", f"refs/tags/{tag}", "--format=%(contents)")
    if annotation.returncode != 0:
        return False
    match = re.search(r"siwah-hmac:([0-9a-f]{64})", annotation.stdout)
    if match is None:
        return False
    key = _load_supervisor_key(create=False)
    if key is None:
        return False
    expected = hmac.new(
        key, _signature_payload(tag, manifest_hash, commit.stdout.strip()), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, match.group(1))


@dataclass(frozen=True)
class SnapshotResult:
    ok: bool
    tag: str | None = None
    detail: str = ""


def _git(repo: Path | str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=Path(repo),
        capture_output=True,
        text=True,
        check=check,
    )


def _git_bytes(repo: Path | str, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=Path(repo), capture_output=True)


def _tag_blob(repo: Path, tag: str, path: str) -> bytes | None:
    result = _git_bytes(repo, "show", f"{tag}:{path}")
    return result.stdout if result.returncode == 0 else None


def _safe_snapshot_path(path: str) -> bool:
    return (
        bool(path) and not path.startswith("/") and "\\" not in path and ".." not in path.split("/")
    )


def _trusted_snapshot(repo: Path, tag: str) -> bool:
    """A healthy tag must be self-consistent with its manifest and name.

    Tags are writable by anyone who can run git, so the *content* is checked:
    the tag name must encode the manifest hash, and every hashed file must match
    the blob stored in that tag. This does not authenticate who created the tag
    (see the residual risk in the audit report), but it rejects tags that do not
    describe their own tree.
    """

    raw = _tag_blob(repo, tag, MANIFEST_NAME)
    if raw is None:
        return False
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(manifest, dict):
        return False
    files_raw = manifest.get("files")
    expected_hash = manifest.get("hash")
    if not isinstance(files_raw, dict) or not isinstance(expected_hash, str):
        return False
    files: dict[str, str] = {}
    for name, digest in files_raw.items():
        if not isinstance(name, str) or not isinstance(digest, str):
            return False
        if not _safe_snapshot_path(name):
            return False
        blob = _tag_blob(repo, tag, name)
        if blob is None:
            return False
        # Git may normalise line endings on commit (core.autocrlf); the manifest
        # records the working-tree bytes. Accept the equivalent LF/CRLF form.
        variants = {blob}
        if b"\r\n" in blob:
            variants.add(blob.replace(b"\r\n", b"\n"))
        else:
            variants.add(blob.replace(b"\n", b"\r\n"))
        if digest not in {hashlib.sha256(value).hexdigest() for value in variants}:
            return False
        files[name] = digest
    if tree_hash(files) != expected_hash or healthy_tag(expected_hash) != tag:
        return False
    listing = _git(repo, "ls-tree", "-r", "--name-only", tag, "--", *PROTECTED_ROLLBACK_PATHS)
    if listing.returncode != 0:
        return False
    tree = {name for name in listing.stdout.splitlines() if name}
    tree.discard(MANIFEST_NAME)
    if tree != set(files):
        return False
    return _tag_signature_valid(repo, tag, expected_hash)


def is_trusted_snapshot(repo: Path | str, manifest_hash: str) -> bool:
    root = Path(repo).resolve()
    if not is_git_repo(root):
        return False
    return _trusted_snapshot(root, healthy_tag(manifest_hash))


def is_git_repo(repo: Path | str) -> bool:
    result = _git(repo, "rev-parse", "--is-inside-work-tree")
    return result.returncode == 0 and result.stdout.strip() == "true"


def healthy_tag(manifest_hash: str) -> str:
    return f"{HEALTHY_PREFIX}{manifest_hash[:12]}"


def create_healthy_snapshot(
    repo: Path | str, manifest_hash: str, *, message: str | None = None
) -> str | None:
    """Tag a clean protected tree; return None when no tag was created."""

    root = Path(repo).resolve()
    if not is_git_repo(root):
        return None
    tag = healthy_tag(manifest_hash)
    existing = _git(root, "rev-parse", "-q", "--verify", f"refs/tags/{tag}")
    if existing.returncode == 0:
        return tag if _trusted_snapshot(root, tag) else None
    status = verify_manifest(root)
    if not status.ok or status.actual_hash != manifest_hash:
        return None
    dirty = _git(root, "status", "--porcelain", "--", *PROTECTED_ROLLBACK_PATHS)
    if dirty.returncode != 0 or dirty.stdout.strip():
        return None
    commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    if not commit:
        return None
    signature = _sign_tag(tag, manifest_hash, commit)
    if signature is None:
        return None
    result = _git(
        root,
        "-c",
        "user.name=siwah-supervisor",
        "-c",
        "user.email=supervisor@localhost",
        "tag",
        "-a",
        tag,
        "-m",
        signature,
    )
    if result.returncode != 0:
        return None
    return tag


def latest_healthy_tag(repo: Path | str, *, exclude: str | None = None) -> str | None:
    root = Path(repo).resolve()
    if not is_git_repo(root):
        return None
    result = _git(root, "tag", "--list", f"{HEALTHY_PREFIX}*", "--sort=-creatordate")
    if result.returncode != 0:
        return None
    tags = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    for candidate in tags:
        if candidate == exclude:
            continue
        if _trusted_snapshot(root, candidate):
            return candidate
    return None


def protected_changes(repo: Path | str) -> list[str]:
    """Return uncommitted changes inside the protected paths."""

    root = Path(repo).resolve()
    result = _git(root, "status", "--porcelain", "--", *PROTECTED_ROLLBACK_PATHS)
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def _tracked_files(repo: Path) -> set[str]:
    result = _git(repo, "ls-files", "-z", "--", *PROTECTED_ROLLBACK_PATHS)
    if result.returncode != 0:
        return set()
    return {value for value in result.stdout.split("\0") if value}


def rollback_to_tag(repo: Path | str, tag: str | None = None) -> SnapshotResult:
    """Restore protected paths from a healthy tag without touching user code."""

    root = Path(repo).resolve()
    if not is_git_repo(root):
        return SnapshotResult(False, tag, "not a git repository")
    if tag is None:
        # A candidate can become "healthy" for a moment before it crashes; never
        # roll back to the tag that represents the currently failing tree. When
        # the manifest is missing/corrupt the tree content itself is not the
        # suspect, so the matching tag is still a valid restore source.
        current_hash = str(build_manifest(root)["hash"])
        exclude = healthy_tag(current_hash) if verify_manifest(root).ok else None
        tag = latest_healthy_tag(root, exclude=exclude)
    target = tag
    if target is None:
        return SnapshotResult(False, None, "no healthy snapshot tag found")
    exists = _git(root, "rev-parse", "-q", "--verify", f"refs/tags/{target}")
    if exists.returncode != 0:
        return SnapshotResult(False, target, f"tag not found: {target}")
    if not _trusted_snapshot(root, target):
        return SnapshotResult(False, target, "tag is not a verified healthy snapshot")

    target_files = {
        name
        for name in _git(
            root, "ls-tree", "-r", "--name-only", "-z", target, "--", *PROTECTED_ROLLBACK_PATHS
        ).stdout.split("\0")
        if name
    }
    current_files = _tracked_files(root)
    extras = sorted(current_files - target_files)
    if extras:
        removed = _git(root, "rm", "-f", "--", *extras)
        if removed.returncode != 0:
            return SnapshotResult(
                False, target, removed.stderr.strip() or "cannot remove extra files"
            )
    # Only pass paths that exist in the tag/working tree: git treats a missing
    # pathspec as an error (e.g. a repository without tests/kernel).
    restore_paths = [
        path
        for path in PROTECTED_ROLLBACK_PATHS
        if any(name == path or name.startswith(path + "/") for name in target_files)
    ]
    if not restore_paths:
        return SnapshotResult(False, target, "tag has no protected files")
    restored = _git(root, "checkout", target, "--", *restore_paths)
    if restored.returncode != 0:
        return SnapshotResult(False, target, restored.stderr.strip() or "checkout failed")
    clean_paths = [path for path in PROTECTED_ROLLBACK_PATHS if (root / path).exists()]
    # -x also removes ignored artifacts (e.g. __pycache__/*.pyc), which CPython
    # would otherwise keep executing after the source has been restored.
    cleaned = _git(root, "clean", "-fdx", "--", *(clean_paths or restore_paths))
    if cleaned.returncode != 0:
        return SnapshotResult(False, target, cleaned.stderr.strip() or "clean failed")
    return SnapshotResult(True, target, "protected paths restored")


__all__ = [
    "HEALTHY_PREFIX",
    "PROTECTED_ROLLBACK_PATHS",
    "SUPERVISOR_KEY_ENV",
    "SnapshotResult",
    "create_healthy_snapshot",
    "healthy_tag",
    "is_git_repo",
    "is_trusted_snapshot",
    "latest_healthy_tag",
    "rollback_to_tag",
]
