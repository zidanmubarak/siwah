"""Shadow-git checkpoints for workspace rewind.

The shadow repository lives outside the user's `.git` and uses the workspace as
its work tree. Every snapshot records the non-ignored workspace state; a rewind
is a hard reset of that shadow repository, which restores tracked files exactly.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path

from ..paths import siwah_home
from .storage import Store

_EXCLUDES = """# Managed by siwah checkpoints.
.git/
.siwah/
.tmp-live/
node_modules/
.venv/
venv/
dist/
build/
target/
__pycache__/
.pytest_cache/
.ruff_cache/
.mypy_cache/
.next/
coverage/
"""


class CheckpointError(RuntimeError):
    pass


class CheckpointManager:
    def __init__(
        self,
        workspace: Path,
        session_id: str,
        *,
        store: Store,
        root: Path | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.session_id = session_id
        self.store = store
        self.git_dir = (root or (siwah_home() / "checkpoints")) / session_id
        self._lock = threading.Lock()
        self._ready = False
        self._enabled = shutil.which("git") is not None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _git(self, *args: str, timeout: float = 30.0, check: bool = True, capture: bool = False):
        # Git for Windows may leave a helper process holding inherited pipes open,
        # which makes capture_output=True hang even after git exits. Only capture
        # stdout for commands whose value is needed;  discard the rest.
        completed = subprocess.run(
            [
                "git",
                "--git-dir",
                str(self.git_dir),
                "--work-tree",
                str(self.workspace),
                *args,
            ],
            cwd=str(self.workspace),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=capture,
            timeout=timeout,
            check=False,
        )
        if check and completed.returncode != 0:
            raise CheckpointError(f"git {' '.join(args)} failed with exit {completed.returncode}")
        return completed

    def ensure(self) -> bool:
        if not self._enabled:
            return False
        if self._ready:
            return True
        with self._lock:
            if self._ready:
                return True
            try:
                if not self.git_dir.exists():
                    self.git_dir.parent.mkdir(parents=True, exist_ok=True)
                    subprocess.run(
                        ["git", "init", "--bare", str(self.git_dir)],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=30,
                        check=True,
                    )
                self._git("config", "core.bare", "false")
                self._git("config", "core.worktree", str(self.workspace))
                self._git("config", "user.name", "siwah")
                self._git("config", "user.email", "siwah@localhost")
                exclude = self.git_dir / "info" / "exclude"
                exclude.parent.mkdir(parents=True, exist_ok=True)
                existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
                installed = set(existing.splitlines())
                missing = [line for line in _EXCLUDES.splitlines() if line not in installed]
                if missing:
                    separator = "\n" if existing and not existing.endswith("\n") else ""
                    exclude.write_text(
                        existing + separator + "\n".join(missing) + "\n", encoding="utf-8"
                    )
                self._ready = True
            except (OSError, subprocess.SubprocessError, CheckpointError):
                self._enabled = False
                self._ready = False
            return self._ready

    def snapshot(self, label: str, *, allow_empty: bool = False) -> str | None:
        if not self.ensure():
            return None
        with self._lock:
            try:
                self._git("add", "-A", "--", ".", timeout=60)
                staged = self._git("diff", "--cached", "--quiet", check=False)
                if staged.returncode == 0 and not allow_empty:
                    return None
                args = ["commit", "--no-gpg-sign", "-m", label or "checkpoint"]
                if allow_empty:
                    args.insert(1, "--allow-empty")
                self._git(*args, timeout=30)
                commit = (self._git("rev-parse", "HEAD", capture=True).stdout or "").strip()
            except (OSError, subprocess.SubprocessError, CheckpointError):
                return None
        if not commit:
            return None
        checkpoint_id = self.store.record_checkpoint(
            session_id=self.session_id,
            commit_hash=commit,
            label=label,
        )
        return checkpoint_id

    def restore(self, commit_hash: str) -> bool:
        if not self.ensure():
            return False
        with self._lock:
            valid = self._git("cat-file", "-e", f"{commit_hash}^{{commit}}", check=False)
            if valid.returncode != 0:
                raise CheckpointError(f"Unknown checkpoint commit: {commit_hash}")
            self._git("reset", "--hard", commit_hash, timeout=60)
        return True

    def initial(self) -> str | None:
        return self.snapshot("initial", allow_empty=True)


__all__ = ["CheckpointError", "CheckpointManager"]
