"""Authoritative candidate validation and diff rendering."""

from __future__ import annotations

import difflib
from pathlib import Path

from .candidates import Candidate
from .gate import check_candidate_safety


def validate_candidate(
    candidate: Candidate, harness_root: Path | str, *, model: str | None = None
) -> list[str]:
    root = Path(harness_root).resolve()
    errors: list[str] = []
    baseline: dict[str, str] = {}
    for path in candidate.target_paths:
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            errors.append(f"target is outside harness root: {path}")
            continue
        if target.exists() and target.is_file():
            baseline[path] = target.read_text(encoding="utf-8")
    if candidate.model and model and candidate.model != model:
        errors.append("model-specific candidate targets a different model")
    errors.extend(
        check_candidate_safety(
            level=candidate.level,
            scope=candidate.scope,
            model=candidate.model,
            target_paths=candidate.target_paths,
            changes=candidate.changes,
            baseline=baseline,
        )
    )
    if not candidate.changes:
        errors.append("candidate has no file changes")
    return errors


def render_diff(candidate: Candidate, harness_root: Path | str) -> str:
    root = Path(harness_root).resolve()
    chunks: list[str] = []
    for path in sorted(candidate.changes):
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"target is outside harness root: {path}")
        before = target.read_text(encoding="utf-8") if target.exists() else ""
        after = candidate.changes[path] or ""
        chunks.extend(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
    return "".join(chunks)


__all__ = ["render_diff", "validate_candidate"]
