"""Version checks and fast-forward updates for a siwah source checkout.

The checker deliberately uses the repository's git remote instead of embedding a
GitHub token. The public repository works unauthenticated; a private fork works
whenever the user's normal git credential helper can authenticate.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .paths import ensure_home, siwah_home
from .version import __version__

DEFAULT_REPO = "zidanmubarak/siwah"
UPDATE_REPO_ENV = "SIWAH_UPDATE_REPO"
UPDATE_SOURCE_ENV = "SIWAH_SOURCE_ROOT"
UPDATE_CACHE_NAME = "update-check.json"
UPDATE_CACHE_TTL_SECONDS = 60 * 60
GIT_TIMEOUT_SECONDS = 15
BUILD_TIMEOUT_SECONDS = 300
_VERSION_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+._]?([0-9A-Za-z.-]+))?$")
_APPLY_LOCK = threading.Lock()


@dataclass(frozen=True)
class UpdateStatus:
    current: str
    latest: str | None = None
    available: bool = False
    repo: str | None = None
    branch: str | None = None
    source: str | None = None
    commits_behind: int = 0
    checked_at: str = ""
    error: str | None = None
    can_apply: bool = False
    detail: str | None = None


@dataclass(frozen=True)
class UpdateApplyResult:
    ok: bool
    from_version: str
    to_version: str | None = None
    detail: str = ""
    restart_required: bool = False
    steps: list[str] = field(default_factory=list)


def version_key(value: str) -> tuple[int, int, int, int, str] | None:
    """Return a sortable key for ``1.2.3`` / ``v1.2.3`` / ``1.2.3-rc1``."""

    match = _VERSION_PATTERN.fullmatch(value.strip())
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.group(1, 2, 3))
    suffix = match.group(4) or ""
    return (major, minor, patch, 0 if not suffix else -1, suffix)


def version_from_pyproject(text: str) -> str | None:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    version = project.get("version")
    return version if isinstance(version, str) else None


def source_root() -> Path | None:
    """Find a siwah source checkout, never an unrelated user repository."""

    candidates: list[Path] = []
    override = os.environ.get(UPDATE_SOURCE_ENV)
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(Path(__file__).resolve().parents[2])
    candidates.append(Path.cwd())
    seen: set[Path] = set()
    for candidate in candidates:
        root = _git_toplevel(candidate)
        if root is None or root in seen:
            continue
        seen.add(root)
        if _is_siwah_checkout(root):
            return root
    return None


def check_for_update(*, refresh: bool = False, root: Path | None = None) -> UpdateStatus:
    """Check the remote branch/version, using a short-lived cache by default."""

    if not refresh:
        cached = _read_cache()
        if cached is not None:
            return cached
    resolved = root or source_root()
    status = _check_checkout(resolved) if resolved is not None else _check_remote_only()
    if status.error is None:
        _write_cache(status)
    return status


def apply_update(*, root: Path | None = None, rebuild_tui: bool = True) -> UpdateApplyResult:
    """Fast-forward the checkout to its upstream branch and optionally rebuild TUI."""

    resolved = root or source_root()
    if resolved is None:
        return UpdateApplyResult(
            ok=False,
            from_version=__version__,
            detail=(
                "This siwah installation is not a source checkout. Reinstall from "
                "the GitHub repository to enable /update."
            ),
        )
    if not _APPLY_LOCK.acquire(blocking=False):
        return UpdateApplyResult(
            ok=False, from_version=__version__, detail="An update is already running."
        )
    try:
        return _apply_checkout(resolved, rebuild_tui=rebuild_tui)
    finally:
        _APPLY_LOCK.release()


def _apply_checkout(root: Path, *, rebuild_tui: bool) -> UpdateApplyResult:
    current = _local_version(root) or __version__
    dirty = _git(root, "status", "--porcelain", "--untracked-files=no")
    if dirty.returncode != 0:
        return UpdateApplyResult(ok=False, from_version=current, detail=dirty.stderr.strip())
    if dirty.stdout.strip():
        return UpdateApplyResult(
            ok=False,
            from_version=current,
            detail="The siwah checkout has local changes. Commit or stash them first.",
        )

    remote = _remote_name(root)
    if remote is None:
        return UpdateApplyResult(
            ok=False, from_version=current, detail="The siwah checkout has no git remote."
        )
    branch = _current_branch(root)
    fetch = _git(root, "fetch", "--tags", "--prune", remote)
    if fetch.returncode != 0:
        return UpdateApplyResult(
            ok=False,
            from_version=current,
            detail=_clean_git_error(fetch, "Could not fetch the update"),
        )

    upstream = f"{remote}/{branch}"
    behind = _commits_behind(root, upstream)
    if behind == 0:
        return UpdateApplyResult(
            ok=True,
            from_version=current,
            to_version=current,
            detail="Already up to date.",
            steps=["No new commits on the upstream branch."],
        )

    merge = _git(root, "merge", "--ff-only", upstream, timeout=60)
    if merge.returncode != 0:
        return UpdateApplyResult(
            ok=False,
            from_version=current,
            detail=_clean_git_error(merge, "Could not fast-forward the checkout"),
        )

    updated = _local_version(root) or current
    steps = [f"Fast-forwarded {behind} commit(s) from {upstream}."]
    installed, install_steps = _reinstall_tool(root)
    steps.extend(install_steps)
    if not installed:
        _clear_cache()
        return UpdateApplyResult(
            ok=False,
            from_version=current,
            to_version=updated,
            detail=(
                "Updated the source, but Python dependencies could not be installed. "
                "Re-run the bootstrap installer or run `uv tool install --force "
                f"--editable {root}`."
            ),
            restart_required=True,
            steps=steps,
        )
    if rebuild_tui:
        steps.extend(_rebuild_tui(root))
    _clear_cache()
    return UpdateApplyResult(
        ok=True,
        from_version=current,
        to_version=updated,
        detail=f"Updated siwah from {current} to {updated}.",
        restart_required=updated != current or behind > 0,
        steps=steps,
    )


def _check_checkout(root: Path) -> UpdateStatus:
    checked_at = _now()
    remote = _remote_name(root)
    branch = _current_branch(root)
    if remote is None:
        return UpdateStatus(
            current=__version__,
            checked_at=checked_at,
            error="The siwah checkout has no git remote.",
            detail="Add the GitHub repository as a git remote.",
        )
    fetch = _git(root, "fetch", "--tags", "--prune", remote)
    fetch_error = None if fetch.returncode == 0 else _clean_git_error(fetch, "Could not fetch")
    upstream = f"{remote}/{branch}"
    latest, source = _remote_version(root, remote, branch)
    behind = _commits_behind(root, upstream)
    current_key = version_key(__version__)
    latest_key = version_key(latest) if latest else None
    available = bool(current_key and latest_key and latest_key > current_key)
    dirty = _git(root, "status", "--porcelain", "--untracked-files=no")
    can_apply = bool(
        latest is not None
        and not fetch_error
        and behind > 0
        and dirty.returncode == 0
        and not dirty.stdout.strip()
    )
    detail = None
    if latest is None:
        detail = "No version tag or pyproject version was found on the upstream branch."
    elif available:
        detail = f"siwah {latest} is available; run /update."
    return UpdateStatus(
        current=__version__,
        latest=latest,
        available=available,
        repo=_remote_repo(root, remote),
        branch=branch,
        source=source,
        commits_behind=behind,
        checked_at=checked_at,
        error=fetch_error,
        can_apply=can_apply,
        detail=detail,
    )


def _check_remote_only() -> UpdateStatus:
    repo = os.environ.get(UPDATE_REPO_ENV, DEFAULT_REPO)
    latest, error = _latest_remote_tag(repo)
    current_key = version_key(__version__)
    latest_key = version_key(latest) if latest else None
    return UpdateStatus(
        current=__version__,
        latest=latest,
        available=bool(current_key and latest_key and latest_key > current_key),
        repo=repo,
        source="ls-remote",
        checked_at=_now(),
        error=error,
        can_apply=False,
        detail=(
            f"siwah {latest} is available. Reinstall from a git checkout to use /update."
            if latest
            else "No version tags were found on the remote."
        ),
    )


def _remote_version(root: Path, remote: str, branch: str) -> tuple[str | None, str | None]:
    show = _git(root, "show", f"{remote}/{branch}:pyproject.toml")
    if show.returncode == 0:
        version = version_from_pyproject(show.stdout)
        if version is not None:
            return version, "pyproject"
    tags = _git(root, "tag", "--list")
    candidates = [
        (key, tag.removeprefix("v"))
        for tag in tags.stdout.splitlines()
        if (key := version_key(tag)) is not None
    ]
    if candidates:
        return max(candidates)[1], "tag"
    return None, None


def _latest_remote_tag(repo: str) -> tuple[str | None, str | None]:
    url = f"https://github.com/{repo}.git"
    env = _git_env()
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--tags", "--refs", url],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if result.returncode != 0:
        return None, result.stderr.strip() or "git ls-remote failed"
    tags: list[tuple[tuple[int, int, int, int, str], str]] = []
    for line in result.stdout.splitlines():
        _sha, _, ref = line.partition("\t")
        tag = ref.removeprefix("refs/tags/")
        key = version_key(tag)
        if key is not None:
            tags.append((key, tag.removeprefix("v")))
    return (max(tags)[1] if tags else None), None


def _rebuild_tui(root: Path) -> list[str]:
    tui = root / "ui" / "tui"
    if not (tui / "package.json").is_file():
        return ["TUI source not found; skipped the TUI rebuild."]
    bun = _find_bun(root)
    if bun is None:
        return [
            "Bun not found; the TUI binary was not rebuilt. Restart with Bun or rebuild it manually."
        ]
    steps: list[str] = []
    install = _run([bun, "install", "--frozen-lockfile"], cwd=tui, timeout=180)
    if not install[0]:
        return [*steps, f"TUI dependency install failed: {install[1]}"]
    steps.append("Reinstalled TUI dependencies.")
    name = "siwah-tui.exe" if os.name == "nt" else "siwah-tui"
    target = siwah_home().resolve() / "bin" / name
    staged = target.with_name(f"{target.name}.next")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".tui-build-", dir=target.parent) as tmp:
            built = Path(tmp) / name
            build = _run(
                [bun, "run", "build"],
                cwd=tui,
                timeout=BUILD_TIMEOUT_SECONDS,
                env={**os.environ, "SIWAH_TUI_OUTFILE": str(built)},
            )
            if not build[0]:
                return [*steps, f"TUI build failed: {build[1]}"]
            if not built.is_file():
                return [*steps, "TUI build did not produce an executable."]
            built.replace(staged)
    except OSError as exc:
        return [*steps, f"Could not stage the TUI binary: {exc}"]
    steps.extend(("Rebuilt the TUI binary.", f"Staged {staged}; it activates on the next launch."))
    return steps


def _reinstall_tool(root: Path) -> tuple[bool, list[str]]:
    uv = _find_uv(root)
    if uv is None:
        return False, [
            "uv was not found; Python dependencies were not installed. "
            "Re-run the bootstrap installer."
        ]
    ok, output = _run(
        [
            uv,
            "pip",
            "install",
            "--python",
            sys.executable,
            "--editable",
            str(root),
        ],
        cwd=root,
        timeout=BUILD_TIMEOUT_SECONDS,
    )
    if not ok:
        return False, [f"Python dependency install failed: {output}"]
    return True, ["Reinstalled Python dependencies."]


def _find_uv(root: Path) -> str | None:
    candidates = [
        shutil.which("uv"),
        str(Path.home() / ".local" / "bin" / ("uv.exe" if os.name == "nt" else "uv")),
        str(Path.home() / ".cargo" / "bin" / ("uv.exe" if os.name == "nt" else "uv")),
        str(root / ".tools" / "uv" / ("uv.exe" if os.name == "nt" else "uv")),
    ]
    return next((path for path in candidates if path and Path(path).is_file()), None)


def _find_bun(root: Path) -> str | None:
    for candidate in (
        shutil.which("bun"),
        str(root / ".tools" / "bun" / ("bun.exe" if os.name == "nt" else "bun")),
        str(Path.home() / ".bun" / "bin" / ("bun.exe" if os.name == "nt" else "bun")),
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def _run(
    command: list[str], *, cwd: Path, timeout: int, env: dict[str, str] | None = None
) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output[-1200:]


def _local_version(root: Path) -> str | None:
    try:
        return version_from_pyproject((root / "pyproject.toml").read_text(encoding="utf-8"))
    except OSError:
        return None


def _is_siwah_checkout(root: Path) -> bool:
    if not (root / ".git").exists() or not (root / "pyproject.toml").is_file():
        return False
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    project = data.get("project")
    return isinstance(project, dict) and project.get("name") == "siwah"


def _git_toplevel(path: Path) -> Path | None:
    if not path.exists():
        return None
    cwd = path if path.is_dir() else path.parent
    result = _git(cwd, "rev-parse", "--show-toplevel")
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve()


def _remote_name(root: Path) -> str | None:
    result = _git(root, "remote")
    remotes = result.stdout.split()
    if "origin" in remotes:
        return "origin"
    return remotes[0] if remotes else None


def _current_branch(root: Path) -> str:
    result = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    branch = result.stdout.strip()
    if branch and branch != "HEAD":
        return branch
    head = _git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if head.returncode == 0 and head.stdout.strip():
        return head.stdout.strip().removeprefix("origin/")
    return "main"


def _remote_repo(root: Path, remote: str) -> str | None:
    result = _git(root, "remote", "get-url", remote)
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    return match.group(1) if match else url


def _commits_behind(root: Path, upstream: str) -> int:
    result = _git(root, "rev-list", "--count", f"HEAD..{upstream}")
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def _git(
    root: Path, *args: str, timeout: int = GIT_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=_git_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(["git", *args], 1, "", str(exc))


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "Never"
    env["GIT_ASKPASS"] = "echo"
    return env


def _clean_git_error(result: subprocess.CompletedProcess[str], fallback: str) -> str:
    detail = (result.stderr or result.stdout).strip()
    return f"{fallback}: {detail}" if detail else fallback


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _cache_path() -> Path:
    return siwah_home() / UPDATE_CACHE_NAME


def _read_cache() -> UpdateStatus | None:
    try:
        raw = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    checked = raw.get("cached_at")
    if not isinstance(checked, (int, float)) or time.time() - checked > UPDATE_CACHE_TTL_SECONDS:
        return None
    data = raw.get("status")
    if not isinstance(data, dict) or data.get("current") != __version__:
        return None
    try:
        return UpdateStatus(**data)
    except TypeError:
        return None


def _write_cache(status: UpdateStatus) -> None:
    try:
        ensure_home()
        _cache_path().write_text(
            json.dumps({"cached_at": time.time(), "status": asdict(status)}, indent=2),
            encoding="utf-8",
        )
    except OSError:
        return


def _clear_cache() -> None:
    try:
        _cache_path().unlink()
    except OSError:
        return
