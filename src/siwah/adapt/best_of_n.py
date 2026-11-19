"""Parallel best-of-N attempts in isolated worktrees."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .worktree import Worktree

AttemptRunner = Callable[[Path, int], Awaitable[bool]]


class BestOfNAttempt(BaseModel):
    model_config = ConfigDict(extra="ignore")

    index: int
    path: str
    passed: bool = False
    error: str | None = None


class BestOfNResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: str
    winner: int | None = None
    attempts: list[BestOfNAttempt] = Field(default_factory=list)
    reason: str | None = None


@dataclass
class _Attempt:
    index: int
    worktree: Worktree
    passed: bool = False
    error: str | None = None
    diff: str = ""
    meta: dict[str, object] = field(default_factory=dict)


class BestOfN:
    def __init__(
        self,
        repo: Path | str,
        *,
        runner: AttemptRunner,
        attempts: int = 3,
    ) -> None:
        self.repo = Path(repo).resolve()
        self.runner = runner
        self.attempts = max(2, attempts)

    async def run(self) -> BestOfNResult:
        if not self._clean():
            return BestOfNResult(status="rejected", reason="repository is dirty")
        created: list[_Attempt] = []
        try:
            for index in range(self.attempts):
                created.append(_Attempt(index=index, worktree=Worktree.create(self.repo)))
            await asyncio.gather(*(self._run_attempt(attempt) for attempt in created))
            winner = next((attempt for attempt in created if attempt.passed), None)
            result = BestOfNResult(
                status="promoted" if winner is not None else "rejected",
                winner=winner.index if winner else None,
                attempts=[
                    BestOfNAttempt(
                        index=attempt.index,
                        path=str(attempt.worktree.path),
                        passed=attempt.passed,
                        error=attempt.error,
                    )
                    for attempt in created
                ],
                reason=None if winner else "no attempt passed verification",
            )
            if winner is not None:
                winner.diff = winner.worktree.diff()
                if not winner.diff.strip():
                    return result.model_copy(
                        update={"status": "rejected", "reason": "winner has no diff"}
                    )
                self._apply(winner.diff)
            return result
        finally:
            for attempt in created:
                attempt.worktree.remove()

    async def _run_attempt(self, attempt: _Attempt) -> None:
        try:
            attempt.passed = await self.runner(attempt.worktree.path, attempt.index)
        except Exception as exc:
            attempt.error = str(exc)
            attempt.passed = False

    def _apply(self, diff: str) -> None:
        patch = self.repo / ".siwah-best-of-n.patch"
        try:
            patch.write_text(diff, encoding="utf-8")
            result = subprocess.run(
                ["git", "apply", "--whitespace=nowarn", str(patch)],
                cwd=self.repo,
                capture_output=True,
                text=True,
            )
        finally:
            patch.unlink(missing_ok=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "cannot apply winning diff")

    def _clean(self) -> bool:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.repo,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and not result.stdout.strip()


__all__ = ["BestOfN", "BestOfNAttempt", "BestOfNResult"]
