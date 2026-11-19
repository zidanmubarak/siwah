"""Harness repository writes, evolution tags, and rollback."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .approval import GateApproval, consume_approval
from .audit import audit_event
from .candidate_safety import render_diff, validate_candidate
from .candidates import Candidate, Promotion

_EVO_TAG = re.compile(r"^evo-(\d+)$")


class PromotionError(RuntimeError):
    pass


class HarnessRepository:
    def __init__(self, root: Path | str, *, git: str = "git") -> None:
        self.root = Path(root).resolve()
        self.git = git

    def _run(self, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.git, *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=check,
        )

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if (self.root / ".git").exists():
            return
        self._run("init", "-q")
        self._run("config", "user.name", "siwah")
        self._run("config", "user.email", "siwah@localhost")
        self._run("add", "-A")
        self._run("commit", "-q", "-m", "harness: initial", check=False)

    def head(self) -> str:
        result = self._run("rev-parse", "HEAD")
        return result.stdout.strip() if result.returncode == 0 else ""

    def clean(self) -> bool:
        result = self._run("status", "--porcelain", "--untracked-files=all")
        return result.returncode == 0 and not result.stdout.strip()

    def baseline(self, paths: list[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for path in paths:
            target = (self.root / path).resolve()
            if target.exists() and target.is_file():
                result[path] = target.read_text(encoding="utf-8")
        return result

    def apply(self, candidate: Candidate) -> list[str]:
        changed: list[str] = []
        for path, content in candidate.changes.items():
            target = (self.root / path).resolve()
            if not _inside(target, self.root):
                raise PromotionError(f"candidate path is outside the harness: {path}")
            if content is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            changed.append(path)
        return changed

    def commit(self, message: str, *, paths: list[str] | None = None) -> str:
        path_args = ["--", *paths] if paths is not None else []
        self._run("--literal-pathspecs", "add", "-A", *path_args, check=True)
        self._run(
            "--literal-pathspecs",
            "-c",
            "user.name=siwah",
            "-c",
            "user.email=siwah@localhost",
            "commit",
            "-q",
            "-m",
            message,
            *(["--only", *path_args] if paths is not None else []),
            check=True,
        )
        return self.head()

    def _evolution_tags(self) -> list[tuple[int, str]]:
        result = self._run("tag", "--list", "evo-*")
        tags: list[tuple[int, str]] = []
        for line in result.stdout.splitlines():
            name = line.strip()
            match = _EVO_TAG.match(name)
            if match:
                tags.append((int(match.group(1)), name))
        return sorted(tags, reverse=True)

    def next_evolution_tag(self) -> str:
        tags = self._evolution_tags()
        return f"evo-{tags[0][0] + 1 if tags else 1}"

    def tag(self, name: str) -> None:
        result = self._run("tag", name)
        if result.returncode != 0:
            raise PromotionError(result.stderr.strip() or f"cannot create tag {name}")

    def rollback(self, tag: str) -> str:
        if not self.clean():
            raise PromotionError("the harness repository must be clean before rollback")
        # --keep refuses concurrent conflicting edits instead of discarding them.
        result = self._run("reset", "--keep", tag)
        if result.returncode != 0:
            raise PromotionError(result.stderr.strip() or f"cannot roll back to {tag}")
        return self.head()

    def latest_evolution_tag(self) -> str | None:
        tags = self._evolution_tags()
        return tags[0][1] if tags else None

    def previous_revision(self, tag: str | None = None) -> str | None:
        tags = self._evolution_tags()
        if tag is None:
            target = tags[0][1] if tags else None
            previous = tags[1][1] if len(tags) > 1 else None
        else:
            target = tag
            previous = None
            for index, (_number, name) in enumerate(tags):
                if name == tag and index + 1 < len(tags):
                    previous = tags[index + 1][1]
                    break
        if previous is not None:
            return previous
        if target is None:
            return None
        result = self._run("rev-parse", f"{target}^")
        return result.stdout.strip() if result.returncode == 0 else None


class Promoter:
    def __init__(
        self,
        repo: HarnessRepository,
        *,
        audit: Callable[..., dict[str, Any]] = audit_event,
    ) -> None:
        self.repo = repo
        self.audit = audit

    def promote(self, candidate: Candidate, *, approval: GateApproval | None = None) -> Promotion:
        if approval is None:
            raise PromotionError("promotion requires a fresh kernel-issued gate approval")
        errors = validate_candidate(candidate, self.repo.root, model=candidate.model)
        if errors:
            raise PromotionError("; ".join(errors))
        self.repo.ensure()
        if not self.repo.clean():
            raise PromotionError("the harness repository must be clean before promotion")
        previous = self.repo.head()
        try:
            consume_approval(approval, self.repo.root, previous, candidate.model_dump_json())
        except RuntimeError as exc:
            raise PromotionError(str(exc)) from exc
        diff = candidate.diff or render_diff(candidate, self.repo.root)
        try:
            changed = self.repo.apply(candidate)
            commit = self.repo.commit(f"evolution: {candidate.id}", paths=changed)
            tag = self.repo.next_evolution_tag()
            self.repo.tag(tag)
        except Exception:
            if previous and self.repo.clean():
                self.repo.rollback(previous)
            # Never discard pending changes on an exceptional path. A failed
            # commit leaves the candidate available for human recovery.
            raise
        promotion = Promotion(
            candidate_id=candidate.id,
            tag=tag,
            commit=commit,
            changed_paths=changed,
            previous_commit=previous,
        )
        self.audit(
            "evolve.promoted",
            {
                "candidate": candidate.id,
                "level": candidate.level,
                "scope": candidate.scope,
                "model": candidate.model,
                "tag": tag,
                "commit": commit,
                "paths": changed,
                "diff": diff[:8_000],
            },
        )
        return promotion

    def rollback(self, tag: str, *, reason: str) -> str:
        commit = self.repo.rollback(tag)
        self.audit("evolve.rollback", {"tag": tag, "commit": commit, "reason": reason})
        return commit


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


__all__ = ["HarnessRepository", "Promoter", "PromotionError"]
