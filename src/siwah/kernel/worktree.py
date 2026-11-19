"""Kernel-owned git worktrees for Level 2 candidate probing."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


class WorktreeError(RuntimeError):
    pass


@dataclass
class CommandResult:
    ok: bool
    output: str
    returncode: int


class Worktree:
    def __init__(self, repo: Path, path: Path, *, commit: str) -> None:
        self.repo = repo
        self.path = path
        self.commit = commit

    @classmethod
    def create(cls, repo: Path | str) -> Worktree:
        root = Path(repo).resolve()
        _git(root, "rev-parse", "--is-inside-work-tree", check=True)
        commit = _git(root, "rev-parse", "HEAD", check=True).stdout.strip()
        temp = Path(tempfile.mkdtemp(prefix="siwah-evo-worktree-"))
        path = temp / "tree"
        result = _git(root, "worktree", "add", "--detach", str(path), commit)
        if result.returncode != 0:
            shutil.rmtree(temp, ignore_errors=True)
            raise WorktreeError(result.stderr.strip() or "cannot create worktree")
        return cls(root, path, commit=commit)

    def apply_patch(self, diff: str) -> None:
        result = _git(self.path, "apply", "--whitespace=nowarn", "-", input_text=diff)
        if result.returncode != 0:
            raise WorktreeError(result.stderr.strip() or "candidate patch does not apply")

    def run(
        self,
        command: list[str],
        *,
        timeout_s: float = 600.0,
        env_extra: dict[str, str] | None = None,
    ) -> CommandResult:
        env = os.environ.copy()
        source = self.path / "src"
        if source.is_dir():
            current = env.get("PYTHONPATH")
            env["PYTHONPATH"] = str(source) + (os.pathsep + current if current else "")
            env["SIWAH_SUBJECT_SOURCE"] = str(source)
        if env_extra:
            env.update(env_extra)
        try:
            completed = subprocess.run(
                command,
                cwd=self.path,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return CommandResult(False, str(exc), 1)
        output = (completed.stdout or "") + (completed.stderr or "")
        return CommandResult(completed.returncode == 0, output, completed.returncode)

    def diff(self) -> str:
        _git(self.path, "add", "-N", ".")
        result = _git(self.path, "diff", "--binary", "--no-ext-diff")
        return result.stdout

    def remove(self) -> None:
        _git(self.repo, "worktree", "remove", "--force", str(self.path))
        shutil.rmtree(self.path.parent, ignore_errors=True)


def _git(
    repo: Path, *args: str, check: bool = False, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        input=input_text.encode("utf-8") if input_text is not None else None,
        check=check,
    )
    return subprocess.CompletedProcess(
        result.args,
        result.returncode,
        result.stdout.decode("utf-8", errors="replace"),
        result.stderr.decode("utf-8", errors="replace"),
    )


__all__ = ["CommandResult", "Worktree", "WorktreeError"]
