"""Immutable gate policy for evolution candidates."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field

from .boundary import PROTECTED_RECOVERY_PATHS
from .eval_models import EvalMetrics
from .policy import GateConfig
from .secrets import secret_marker

_ALLOWED_PREFIXES = (
    "prompts/",
    "tool_guidance/",
    "skills/",
    "profiles/",
    "subagents/",
    "commands/",
    "hooks/",
    "config.toml",
)
_PROTECTED_PREFIXES = (
    "src/siwah/kernel",
    "evals",
    "tests/kernel",
    "kernel.manifest.json",
)
_UNSAFE_PHRASES = (
    "ignore permission",
    "ignore the permission",
    "bypass guard",
    "bypass the guard",
    "disable verifier",
    "disable the verifier",
    "skip confirmation",
    "skip the confirmation",
    "do not verify",
    "yolo mode",
)
_GUARD_SYMBOLS = (
    "_is_catastrophic_command",
    "check_candidate_safety",
    "check_level2_diff",
    "deny_paths",
    "is_protected",
    "protected_paths",
    "rollback_to_tag",
    "verify_chain",
    "verify_manifest",
)
_MODE_RANK = {"ask": 0, "auto-edit": 1, "yolo": 2}
LEVEL2_MAX_FILES = 5
LEVEL2_MAX_LINES = 200
LEVEL2_FORBIDDEN_PREFIXES = tuple(path + "/" for path in PROTECTED_RECOVERY_PATHS) + ("ui/",)


@dataclass(frozen=True)
class GateDecision:
    accepted: bool
    reasons: list[str] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)
    delta_pass_rate: float = 0.0
    cost_ratio: float = 0.0

    def summary(self) -> str:
        state = "accepted" if self.accepted else "rejected"
        return f"{state}: " + ("; ".join(self.reasons) if self.reasons else "all checks passed")


@dataclass(frozen=True)
class DiffFile:
    path: str
    added: int = 0
    removed: int = 0


@dataclass(frozen=True)
class DiffStats:
    files: list[DiffFile]
    added: int
    removed: int

    @property
    def changed_lines(self) -> int:
        return self.added + self.removed


def parse_unified_diff(diff: str) -> DiffStats:
    """Parse file/line counts and paths from a unified diff."""

    files: dict[str, DiffFile] = {}
    current: str | None = None
    old_path: str | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current = None
            old_path = None
            continue
        if line.startswith("--- "):
            old_path = _diff_path(line[4:]) or None
            continue
        if line.startswith("+++ "):
            new_path = _diff_path(line[4:])
            current = new_path or old_path
            if current:
                files.setdefault(current, DiffFile(path=current))
            continue
        if current is None:
            continue
        item = files[current]
        if line.startswith("+") and not line.startswith("+++"):
            files[current] = DiffFile(item.path, item.added + 1, item.removed)
        elif line.startswith("-") and not line.startswith("---"):
            files[current] = DiffFile(item.path, item.added, item.removed + 1)
    ordered = sorted(files.values(), key=lambda item: item.path)
    return DiffStats(
        files=ordered,
        added=sum(item.added for item in ordered),
        removed=sum(item.removed for item in ordered),
    )


def _diff_path(raw: str) -> str:
    value = raw.split("\t", 1)[0].strip()
    if value == "/dev/null":
        return ""
    if value.startswith(("a/", "b/")):
        value = value[2:]
    return value


def check_level2_diff(
    diff: str,
    *,
    max_files: int = LEVEL2_MAX_FILES,
    max_lines: int = LEVEL2_MAX_LINES,
) -> tuple[DiffStats, list[str]]:
    """Validate a Level 2 code patch before it reaches a worktree."""

    stats = parse_unified_diff(diff)
    errors: list[str] = []
    if not stats.files:
        errors.append("Level 2 diff contains no files")
    if len(stats.files) > max_files:
        errors.append(f"Level 2 diff changes {len(stats.files)} files; limit is {max_files}")
    if stats.changed_lines > max_lines:
        errors.append(f"Level 2 diff changes {stats.changed_lines} lines; limit is {max_lines}")
    seen_paths: set[str] = set()
    for line in diff.splitlines():
        path = ""
        if line.startswith(("--- ", "+++ ")):
            path = _diff_path(line[4:])
        elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            path = line.split(" ", 2)[2].strip()
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        if not _safe_relative(path) or path.startswith("/"):
            errors.append(f"unsafe Level 2 path: {path}")
            continue
        if _is_forbidden_level2_path(path):
            errors.append(f"forbidden Level 2 path: {path}")

    for item in stats.files:
        path = item.path
        if path.startswith("tests/") and not path.startswith("tests/kernel/") and item.removed:
            errors.append(f"Level 2 patch may not weaken existing tests: {path}")
    for line in diff.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("-"):
            removed = line[1:].casefold()
            for symbol in _GUARD_SYMBOLS:
                if symbol in removed:
                    errors.append(f"Level 2 diff removes guard logic: {symbol}")
                    break
            continue
        if not line.startswith("+"):
            continue
        added = line[1:]
        if secret_marker(added):
            errors.append("Level 2 diff contains a credential-like value")
            break
        lowered = added.lower()
        for phrase in _UNSAFE_PHRASES:
            if phrase in lowered:
                errors.append(f"Level 2 diff appears to loosen a guard: {phrase}")
                break
    if 'permissions.mode = "yolo"' in diff or "permissions.mode = 'yolo'" in diff:
        errors.append("Level 2 diff loosens the permission mode")
    return stats, errors


def _normalized_level2_path(path: str) -> str:
    """Normalize a diff path the way a case-insensitive filesystem would."""

    segments = [segment.rstrip(" .") for segment in path.split("/")]
    return "/".join(segments).casefold()


def _is_forbidden_level2_path(path: str) -> bool:
    normalized = _normalized_level2_path(path)
    return any(
        normalized == prefix.rstrip("/").casefold() or normalized.startswith(prefix.casefold())
        for prefix in LEVEL2_FORBIDDEN_PREFIXES
    )


def _safe_relative(path: str) -> bool:
    return (
        bool(path)
        and not path.startswith("/")
        and "\\" not in path
        and ".." not in path.split("/")
        and re.fullmatch(r"[A-Za-z0-9._/-]+", path) is not None
    )


def _profile_prefix(model: str) -> str:
    provider, separator, name = model.partition("/")
    if not separator:
        return ""
    return f"profiles/{provider}__{name}/"


def check_candidate_safety(
    *,
    level: int,
    scope: str,
    model: str | None,
    target_paths: list[str],
    changes: Mapping[str, str | None],
    baseline: Mapping[str, str] | None = None,
) -> list[str]:
    """Return safety errors for a candidate before any probe or write."""

    errors: list[str] = []
    if level not in {0, 1}:
        errors.append("evolution accepts only Level 0 and Level 1 candidates")
    if scope not in {"global", "project", "model"}:
        errors.append(f"unknown candidate scope '{scope}'")
    if sorted(target_paths) != sorted(changes.keys()):
        errors.append("target_paths must match changed files")

    profile_prefix = _profile_prefix(model) if model else ""
    for path in changes:
        if not _safe_relative(path):
            errors.append(f"unsafe candidate path: {path}")
            continue
        if any(path == prefix or path.startswith(prefix + "/") for prefix in _PROTECTED_PREFIXES):
            errors.append(f"candidate may not touch protected path: {path}")
        if not any(path == prefix or path.startswith(prefix) for prefix in _ALLOWED_PREFIXES):
            errors.append(f"candidate path is outside editable harness data: {path}")
        if scope == "model":
            if not profile_prefix or not path.startswith(profile_prefix):
                errors.append(
                    f"model candidate must write under {profile_prefix or 'profiles/<provider>__<model>/'}"
                )
        elif path.startswith("profiles/"):
            errors.append(f"global/project candidate may not write a model profile: {path}")

    for path, after in changes.items():
        if after is None:
            continue
        marker = secret_marker(after)
        if marker:
            errors.append(f"candidate file {path} contains a credential-like value")
        lowered = after.lower()
        for phrase in _UNSAFE_PHRASES:
            if phrase in lowered:
                errors.append(f"candidate file {path} appears to loosen a guard: {phrase}")

    config_after = changes.get("config.toml")
    config_before = (baseline or {}).get("config.toml")
    if config_after and config_before:
        errors.extend(_config_safety(config_before, config_after))
    return errors


def _config_safety(before: str, after: str) -> list[str]:
    errors: list[str] = []
    try:
        old = tomllib.loads(before)
        new = tomllib.loads(after)
    except tomllib.TOMLDecodeError as exc:
        return [f"candidate config is not valid TOML: {exc}"]
    for section in ("gate", "budget", "canary"):
        if old.get(section, {}) != new.get(section, {}):
            errors.append(f"candidate may not change human-owned {section} policy")
    old_permissions = old.get("permissions", {}) if isinstance(old.get("permissions"), dict) else {}
    new_permissions = new.get("permissions", {}) if isinstance(new.get("permissions"), dict) else {}
    old_mode = str(old_permissions.get("mode", "ask"))
    new_mode = str(new_permissions.get("mode", "ask"))
    if _MODE_RANK.get(new_mode, 0) > _MODE_RANK.get(old_mode, 0):
        errors.append("candidate loosens the permission mode")
    old_deny = old_permissions.get("deny_paths")
    new_deny = new_permissions.get("deny_paths")
    if old_deny and (not new_deny or len(new_deny) < len(old_deny)):
        errors.append("candidate removes deny paths")
    if 'decision = "allow"' in after and 'decision = "allow"' not in before:
        errors.append("candidate adds an allow permission rule")
    return errors


def _min_gain(baseline: EvalMetrics, candidate: EvalMetrics, expression: str) -> tuple[bool, str]:
    if "pass_rate_up" not in expression:
        return False, "gate min_gain expression is unsupported"
    pass_up = candidate.pass_rate > baseline.pass_rate
    equal = abs(candidate.pass_rate - baseline.pass_rate) < 1e-9
    steps_down = baseline.steps > 0 and (baseline.steps - candidate.steps) / baseline.steps >= 0.10
    cost_down = (
        baseline.allowance > 0
        and (baseline.allowance - candidate.allowance) / baseline.allowance >= 0.10
    )
    if "steps_or_cost_down" in expression and "equal pass_rate" in expression:
        return pass_up or (equal and (steps_down or cost_down)), (
            "pass rate up" if pass_up else "steps or cost down with equal pass rate"
        )
    return pass_up, "pass rate up"


def evaluate_gate(
    baseline: EvalMetrics,
    candidate: EvalMetrics,
    config: GateConfig,
    *,
    heldout_baseline: EvalMetrics | None = None,
    heldout_candidate: EvalMetrics | None = None,
) -> GateDecision:
    checks: dict[str, bool] = {}
    reasons: list[str] = []
    checks["candidate_has_tasks"] = candidate.tasks > 0
    if not checks["candidate_has_tasks"]:
        reasons.append("candidate probe produced no tasks")
    checks["golden_regression"] = (
        candidate.pass_rate >= baseline.pass_rate - config.golden_regression_tolerance
    )
    if not checks["golden_regression"]:
        reasons.append("golden pass rate regressed")
    if heldout_baseline is not None and heldout_candidate is not None:
        checks["heldout_noninferiority"] = (
            heldout_candidate.pass_rate
            >= heldout_baseline.pass_rate - config.heldout_noninferiority
        )
        if not checks["heldout_noninferiority"]:
            reasons.append("heldout pass rate is inferior")
    gain, gain_reason = _min_gain(baseline, candidate, config.min_gain)
    checks["min_gain"] = gain
    if not gain:
        reasons.append(f"minimum gain not met ({gain_reason})")
    cost_ratio = candidate.allowance / baseline.allowance if baseline.allowance > 0 else 0.0
    checks["max_cost_ratio"] = cost_ratio <= config.max_cost_ratio
    if not checks["max_cost_ratio"]:
        reasons.append(f"cost ratio {cost_ratio:.2f} exceeds {config.max_cost_ratio:.2f}")
    return GateDecision(
        accepted=all(checks.values()),
        reasons=reasons,
        checks=checks,
        delta_pass_rate=candidate.pass_rate - baseline.pass_rate,
        cost_ratio=cost_ratio,
    )


__all__ = [
    "DiffFile",
    "DiffStats",
    "GateDecision",
    "check_candidate_safety",
    "check_level2_diff",
    "evaluate_gate",
    "parse_unified_diff",
]
