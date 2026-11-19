"""Level 2 core-code evolution: worktree gate, promotion, and rollback."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .approval import GateApproval, _issue_approval, consume_approval
from .audit import audit_event
from .candidates import Candidate
from .gate import DiffStats, check_level2_diff
from .manifest import verify_manifest
from .policy import GateConfig
from .restart import RestartController
from .worktree import Worktree

_CORE_TAG = re.compile(r"^evo-core-(\d+)$")


def _patch_paths(diff: str) -> list[str]:
    paths: set[str] = set()
    for line in diff.splitlines():
        if line.startswith(("--- ", "+++ ")):
            path = line[4:].split("\t", 1)[0].strip()
            if path.startswith(("a/", "b/")):
                path = path[2:]
        elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            path = line.split(" ", 2)[2].strip()
        else:
            continue
        if path != "/dev/null":
            paths.add(path)
    return sorted(paths)


class CorePromotion(BaseModel):
    tag: str
    commit: str
    previous_commit: str


class Level2Result(BaseModel):
    model_config = ConfigDict(extra="ignore", arbitrary_types_allowed=True)

    status: str
    candidate_id: str = ""
    stats: DiffStats | None = None
    checks: dict[str, bool] = Field(default_factory=dict)
    outputs: dict[str, str] = Field(default_factory=dict)
    promotion: CorePromotion | None = None
    reason: str | None = None


@dataclass(frozen=True)
class GateCommand:
    name: str
    command: list[str]
    timeout_s: float = 600.0


class CoreRepository:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()

    def _run(
        self, *args: str, check: bool = False, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *args],
            cwd=self.root,
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

    def clean(self) -> bool:
        result = self._run("status", "--porcelain", "--untracked-files=all")
        return result.returncode == 0 and not result.stdout.strip()

    def head(self) -> str:
        result = self._run("rev-parse", "HEAD")
        return result.stdout.strip() if result.returncode == 0 else ""

    def promote(
        self, diff: str, candidate_id: str, *, approval: GateApproval | None = None
    ) -> CorePromotion:
        if not self.clean():
            raise RuntimeError("the repository must be clean before promotion")
        _stats, errors = check_level2_diff(diff)
        if errors:
            raise RuntimeError("; ".join(errors))
        previous = self.head()
        consume_approval(approval, self.root, previous, diff)
        try:
            # --index stages only the patch's paths; stdin cannot become a
            # tracked artifact, and application fails on conflicting edits.
            applied = self._run("apply", "--index", "--whitespace=nowarn", "-", input_text=diff)
            if applied.returncode != 0:
                raise RuntimeError(applied.stderr.strip() or "patch does not apply")
            committed = self._run(
                "--literal-pathspecs",
                "-c",
                "user.name=siwah",
                "-c",
                "user.email=siwah@localhost",
                "commit",
                "-q",
                "-m",
                f"evolution: {candidate_id}",
                "--only",
                "--",
                *_patch_paths(diff),
            )
            if committed.returncode != 0:
                raise RuntimeError(committed.stderr.strip() or "commit failed")
            commit = self.head()
            tag = self.next_tag()
            tagged = self._run("tag", tag)
            if tagged.returncode != 0:
                raise RuntimeError(tagged.stderr.strip() or "tag failed")
        except Exception:
            if previous and self.clean():
                self.rollback(previous)
            # Dirty state is preserved for recovery, never reset away.
            raise
        return CorePromotion(tag=tag, commit=commit, previous_commit=previous)

    def next_tag(self) -> str:
        result = self._run("tag", "--list", "evo-core-*")
        numbers = []
        for line in result.stdout.splitlines():
            match = _CORE_TAG.match(line.strip())
            if match:
                numbers.append(int(match.group(1)))
        return f"evo-core-{max(numbers, default=0) + 1}"

    def rollback(self, commit: str, *, tag: str | None = None) -> None:
        if not self.clean():
            raise RuntimeError("the repository must be clean before rollback")
        result = self._run("reset", "--keep", commit)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "rollback failed")
        if tag:
            self._run("tag", "-d", tag)


def default_level2_commands(gate: GateConfig | None = None) -> list[GateCommand]:
    python = sys.executable
    source = Path(__file__).resolve().parents[2]
    evaluator = (
        "import sys; "
        f"sys.path.insert(0, {str(source)!r}); "
        "from siwah.kernel.eval_cli import main; main()"
    )

    def eval_command(tier: str, suite: str = "golden") -> list[str]:
        return [
            python,
            "-I",
            "-B",
            "-c",
            evaluator,
            "--tier",
            tier,
            "--suite",
            suite,
            "--baseline-source",
            str(source),
            "--policy",
            (gate or GateConfig()).model_dump_json(),
        ]

    return [
        GateCommand("pytest", [python, "-m", "pytest", "-q"]),
        GateCommand("ruff", [python, "-m", "ruff", "check", "."], timeout_s=120),
        GateCommand("pyright", [python, "-m", "pyright"], timeout_s=300),
        GateCommand("smoke", [python, "-m", "siwah", "--version"], timeout_s=60),
        GateCommand("eval-t0", eval_command("t0")),
        GateCommand("eval-t1", eval_command("t1")),
        GateCommand("eval-t2", eval_command("t2")),
        GateCommand("eval-t3", eval_command("t3")),
    ]


class Level2Evolver:
    def __init__(
        self,
        repo: Path | str,
        *,
        commands: list[GateCommand] | None = None,
        gate: GateConfig | None = None,
        restart: Callable[[], bool] | None = None,
        post_check: Callable[[Path], bool] | None = None,
        audit: Callable[..., dict[str, Any]] = audit_event,
        testing: bool = False,
    ) -> None:
        if commands is not None and not testing:
            raise ValueError("custom gate commands are permitted only in explicit test fixtures")
        if commands is not None and not commands:
            raise ValueError("the gate command list must not be empty")
        self.repo = CoreRepository(repo)
        self.gate = gate or GateConfig()
        self.commands = commands if commands is not None else default_level2_commands(self.gate)
        self.restart = restart or RestartController()
        self.post_check = post_check or _default_post_check
        self.audit = audit
        self.testing = testing

    def run(self, candidate: Candidate) -> Level2Result:
        stats, errors = check_level2_diff(candidate.diff)
        if candidate.level != 2:
            errors.append("Level2Evolver requires a Level 2 candidate")
        if errors:
            self.audit("evolve.level2.rejected", {"candidate": candidate.id, "errors": errors})
            return Level2Result(
                status="rejected",
                candidate_id=candidate.id,
                stats=stats,
                reason="; ".join(errors),
            )
        if not self.testing:
            integrity = verify_manifest(self.repo.root)
            if not integrity.ok:
                return Level2Result(
                    status="rejected",
                    candidate_id=candidate.id,
                    reason=integrity.summary(),
                )
        if not self.repo.clean():
            return Level2Result(
                status="rejected",
                candidate_id=candidate.id,
                stats=stats,
                reason="repository is dirty",
            )
        worktree = Worktree.create(self.repo.root)
        checks: dict[str, bool] = {}
        outputs: dict[str, str] = {}
        try:
            worktree.apply_patch(candidate.diff)
            if not self.testing and not verify_manifest(worktree.path).ok:
                return Level2Result(
                    status="rejected",
                    candidate_id=candidate.id,
                    reason="candidate changed protected enforcement",
                )
            for stage in self.commands:
                result = worktree.run(stage.command, timeout_s=stage.timeout_s)
                checks[stage.name] = result.ok
                outputs[stage.name] = result.output[-8_000:]
                if not result.ok:
                    self.audit(
                        "evolve.level2.rejected",
                        {
                            "candidate": candidate.id,
                            "stage": stage.name,
                            "output": outputs[stage.name],
                        },
                    )
                    return Level2Result(
                        status="rejected",
                        candidate_id=candidate.id,
                        stats=stats,
                        checks=checks,
                        outputs=outputs,
                        reason=f"gate stage failed: {stage.name}",
                    )
        except Exception as exc:
            return Level2Result(
                status="rejected",
                candidate_id=candidate.id,
                stats=stats,
                checks=checks,
                outputs=outputs,
                reason=str(exc),
            )
        finally:
            worktree.remove()

        # The main repository and patch must still match what was checked.
        if self.repo.head() != worktree.commit or (
            not self.testing and not verify_manifest(self.repo.root).ok
        ):
            return Level2Result(
                status="rejected", candidate_id=candidate.id, reason="HEAD changed during gate"
            )
        approval = _issue_approval(
            self.repo.root,
            worktree.commit,
            candidate.diff,
            policy=self.gate.model_dump_json(),
        )
        promotion = self.repo.promote(candidate.diff, candidate.id, approval=approval)
        self.audit(
            "evolve.level2.promoted",
            {
                "candidate": candidate.id,
                "tag": promotion.tag,
                "commit": promotion.commit,
                "files": [item.path for item in stats.files],
                "checks": checks,
            },
        )
        try:
            boot_ok = bool(self.post_check(self.repo.root))
        except Exception:
            boot_ok = False
        if not boot_ok:
            return self._rollback_result(
                candidate, stats, checks, outputs, promotion, "post-promotion boot check failed"
            )
        try:
            restarted = bool(self.restart())
        except Exception:
            restarted = False
        if not restarted:
            return self._rollback_result(
                candidate, stats, checks, outputs, promotion, "supervisor restart failed"
            )
        return Level2Result(
            status="promoted",
            candidate_id=candidate.id,
            stats=stats,
            checks=checks,
            outputs=outputs,
            promotion=promotion,
        )

    def _rollback_result(
        self,
        candidate: Candidate,
        stats: DiffStats,
        checks: dict[str, bool],
        outputs: dict[str, str],
        promotion: CorePromotion,
        reason: str,
    ) -> Level2Result:
        try:
            self.repo.rollback(promotion.previous_commit, tag=promotion.tag)
        except RuntimeError as exc:
            status = "rollback_blocked"
            reason = f"{reason}; rollback blocked: {exc}; user changes were preserved"
        else:
            status = "rolled_back"
        self.audit(
            "evolve.level2.rollback"
            if status == "rolled_back"
            else "evolve.level2.rollback_blocked",
            {"candidate": candidate.id, "reason": reason},
        )
        return Level2Result(
            status=status,
            candidate_id=candidate.id,
            stats=stats,
            checks=checks,
            outputs=outputs,
            promotion=promotion,
            reason=reason,
        )


def _default_post_check(repo: Path) -> bool:
    source = repo / "src"
    if not source.is_dir():
        return True
    env = os.environ.copy()
    current = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(source) + (os.pathsep + current if current else "")
    result = subprocess.run(
        [sys.executable, "-m", "siwah", "--version"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.returncode == 0


__all__ = [
    "CorePromotion",
    "CoreRepository",
    "GateCommand",
    "Level2Evolver",
    "Level2Result",
    "default_level2_commands",
]
