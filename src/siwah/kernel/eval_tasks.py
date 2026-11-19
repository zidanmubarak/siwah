"""Load and validate eval task documents."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .eval_models import EvalError, EvalTask, MockAction, Tier
from .secrets import secret_marker

SUITES = {"golden", "heldout", "traps"}
HELDOUT_DIR_ENV = "SIWAH_HELDOUT_DIR"
CATEGORIES = {
    "bugfix",
    "feature",
    "refactor",
    "tests",
    "multi-file",
    "comprehension",
    "tool-hygiene",
    "trap",
}
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{2,80}$")
_SAFE_PATH = re.compile(r"^(?!.*\.\.)[A-Za-z0-9._/-]+$")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def eval_root(path: Path | str | None = None) -> Path:
    if path is not None:
        root = Path(path).expanduser().resolve()
    elif os.environ.get("SIWAH_EVAL_DIR"):
        root = Path(os.environ["SIWAH_EVAL_DIR"]).expanduser().resolve()
    else:
        candidates = [repo_root() / "evals", Path.cwd() / "evals"]
        root = next((item for item in candidates if item.is_dir()), candidates[0])
    if not root.is_dir():
        raise EvalError(f"Eval directory not found: {root}")
    return root


def suite_root(suite: str, root: Path | str | None = None) -> Path:
    """Resolve one suite directory without leaking private heldout tasks.

    An explicit ``root`` always wins so callers such as tests and the protected
    Level 2 evaluator can pin their fixture tree. Without one, ``heldout`` can
    be loaded from ``SIWAH_HELDOUT_DIR``; public installs ship only samples.
    """

    if suite not in SUITES:
        raise EvalError(f"Unknown eval suite '{suite}'; expected one of {sorted(SUITES)}")
    if root is not None:
        return Path(root).expanduser().resolve() / suite
    if suite == "heldout":
        external = os.environ.get(HELDOUT_DIR_ENV)
        if external:
            directory = Path(external).expanduser().resolve()
            if not directory.is_dir():
                raise EvalError(f"{HELDOUT_DIR_ENV} is not a directory: {directory}")
            return directory
    return eval_root() / suite


def load_tasks(suite: str, root: Path | str | None = None) -> list[EvalTask]:
    if suite not in SUITES:
        raise EvalError(f"Unknown eval suite '{suite}'; expected one of {sorted(SUITES)}")
    directory = suite_root(suite, root)
    if not directory.is_dir():
        hint = f"; set {HELDOUT_DIR_ENV} to the full heldout suite" if suite == "heldout" else ""
        raise EvalError(f"Eval suite directory not found: {directory}{hint}")
    tasks: list[EvalTask] = []
    seen: set[str] = set()
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EvalError(f"Cannot read eval task {path}: {exc}") from exc
        try:
            task = EvalTask.model_validate(payload)
        except Exception as exc:
            raise EvalError(f"Invalid eval task {path}: {exc}") from exc
        if task.id in seen:
            raise EvalError(f"Duplicate eval task id: {task.id}")
        seen.add(task.id)
        errors = validate_task(task)
        if errors:
            raise EvalError(f"Invalid eval task {path}: " + "; ".join(errors))
        tasks.append(task)
    if not tasks:
        raise EvalError(f"No eval tasks found in {directory}")
    return tasks


def validate_task(task: EvalTask) -> list[str]:
    errors: list[str] = []
    if not _ID.fullmatch(task.id):
        errors.append("id must be lowercase letters/digits/._- and 3-80 chars")
    if task.category not in CATEGORIES:
        errors.append(f"unknown category '{task.category}'")
    if not task.prompt.strip():
        errors.append("prompt is empty")
    if not task.files:
        errors.append("files is empty")
    for name, content in task.files.items():
        if not _SAFE_PATH.fullmatch(name) or name.startswith("/"):
            errors.append(f"unsafe file path: {name}")
        if not isinstance(content, str):
            errors.append(f"file content for {name} is not text")
        elif secret_marker(content):
            errors.append(f"file {name} contains a credential-like value")
    for name in task.protected:
        if not _SAFE_PATH.fullmatch(name) or name.startswith("/"):
            errors.append(f"unsafe protected path: {name}")
        elif name not in task.files:
            errors.append(f"protected file is not in files: {name}")
    if not task.check and not task.check_text:
        errors.append("check or check_text is required")
    if task.check and "{python}" not in task.check and "{bun}" not in task.check:
        errors.append("check must contain a {python} or {bun} placeholder")
    if task.check_text is not None and not task.check_text.strip():
        errors.append("check_text is empty")
    for action in [*task.mock.first_tools, *task.mock.repair_tools]:
        _validate_action(action, errors)
    if task.trap is not None:
        if task.trap.marker_file and (
            not _SAFE_PATH.fullmatch(task.trap.marker_file) or task.trap.marker_file.startswith("/")
        ):
            errors.append(f"unsafe trap marker file: {task.trap.marker_file}")
        if not task.trap.marker_file and not task.trap.marker_text:
            errors.append("trap needs marker_file or marker_text")
    return errors


def _validate_action(action: MockAction, errors: list[str]) -> None:
    if not action.name.strip():
        errors.append("mock action name is empty")
    if not isinstance(action.arguments, dict):
        errors.append(f"mock action {action.name} arguments must be an object")


def validate_suite(suite: str, root: Path | str | None = None) -> list[str]:
    try:
        load_tasks(suite, root)
    except EvalError as exc:
        return [str(exc)]
    return []


def select_tier(tasks: list[EvalTask], tier: Tier, limit: int | None = None) -> list[EvalTask]:
    selected = list(tasks)
    if tier == Tier.T1:
        selected = selected[:6]
    elif tier == Tier.T2:
        preferred = {
            "bugfix",
            "feature",
            "refactor",
            "multi-file",
            "tests",
            "comprehension",
        }
        selected = [task for task in selected if task.category in preferred][:12]
    if limit is not None and limit > 0:
        selected = selected[:limit]
    return selected


def task_statistics(tasks: list[EvalTask]) -> dict[str, object]:
    languages: dict[str, int] = {}
    difficulties: dict[str, int] = {}
    categories: dict[str, int] = {}
    for task in tasks:
        languages[task.language] = languages.get(task.language, 0) + 1
        difficulties[task.difficulty] = difficulties.get(task.difficulty, 0) + 1
        categories[task.category] = categories.get(task.category, 0) + 1
    return {
        "count": len(tasks),
        "languages": languages,
        "difficulties": difficulties,
        "categories": categories,
        "traps": sum(1 for task in tasks if task.trap is not None),
    }


__all__ = [
    "CATEGORIES",
    "HELDOUT_DIR_ENV",
    "SUITES",
    "eval_root",
    "load_tasks",
    "repo_root",
    "select_tier",
    "suite_root",
    "task_statistics",
    "validate_suite",
    "validate_task",
]
