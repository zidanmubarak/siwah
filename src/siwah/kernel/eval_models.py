"""Immutable evaluation task, result, and report schemas."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Tier(StrEnum):
    T0 = "t0"
    T1 = "t1"
    T2 = "t2"
    T3 = "t3"


class MockAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class MockScript(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_tools: list[MockAction] = Field(default_factory=list)
    repair_tools: list[MockAction] = Field(default_factory=list)
    requires_verifier: bool = False
    final: str = "Done."


class TrapSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["reward_hacking", "prompt_injection", "destructive"]
    marker_file: str | None = None
    marker_text: str | None = None


class EvalTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category: str
    language: Literal["python", "typescript"]
    difficulty: Literal["easy", "medium", "hard"]
    prompt: str
    files: dict[str, str] = Field(default_factory=dict)
    protected: list[str] = Field(default_factory=list)
    check: str | None = None
    check_text: str | None = None
    mock: MockScript = Field(default_factory=MockScript)
    trap: TrapSpec | None = None


class EvalTaskResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    category: str
    language: str
    difficulty: str
    passed: bool = False
    check_ok: bool = False
    check_text_ok: bool = True
    guard_violation: bool = False
    trap_triggered: bool = False
    steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_read_ratio: float = 0.0
    allowance: float = 0.0
    duration_s: float = 0.0
    tool_calls: int = 0
    tool_errors: int = 0
    error: str | None = None
    check_output: str = ""

    def stable(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "language": self.language,
            "difficulty": self.difficulty,
            "passed": self.passed,
            "check_ok": self.check_ok,
            "check_text_ok": self.check_text_ok,
            "guard_violation": self.guard_violation,
            "trap_triggered": self.trap_triggered,
            "steps": self.steps,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_read_ratio": round(self.cache_read_ratio, 6),
            "allowance": round(self.allowance, 9),
            "tool_calls": self.tool_calls,
            "tool_errors": self.tool_errors,
        }


class EvalMetrics(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tasks: int = 0
    passed: int = 0
    pass_rate: float = 0.0
    steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_read_ratio: float = 0.0
    allowance: float = 0.0
    duration_s: float = 0.0
    tool_calls: int = 0
    tool_errors: int = 0
    tool_error_rate: float = 0.0
    trap_triggered: int = 0
    guard_violations: int = 0

    def stable(self) -> dict[str, Any]:
        return {
            "tasks": self.tasks,
            "passed": self.passed,
            "pass_rate": round(self.pass_rate, 6),
            "steps": self.steps,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_read_ratio": round(self.cache_read_ratio, 6),
            "allowance": round(self.allowance, 9),
            "tool_calls": self.tool_calls,
            "tool_errors": self.tool_errors,
            "tool_error_rate": round(self.tool_error_rate, 6),
            "trap_triggered": self.trap_triggered,
            "guard_violations": self.guard_violations,
        }


class BudgetSuggestion(BaseModel):
    max_allowance_per_cycle: float
    max_cycles_per_day: int = 3
    reserve_weekly_pct: int = 40
    max_concurrency: int = 2
    cooldown_minutes: int = 60
    max_wall_minutes: int = 1


class EvalReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    suite: str
    harness: str
    model: str
    tier: str
    seed: int
    runs_per_task: int
    max_steps: int = 8
    started_at: str
    duration_s: float
    tasks: list[EvalTaskResult] = Field(default_factory=list)
    metrics: EvalMetrics = Field(default_factory=EvalMetrics)
    budget_suggestion: BudgetSuggestion
    cache_hits: int = 0
    validation_errors: list[str] = Field(default_factory=list)
    stopped_early: bool = False

    def stable_projection(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "harness": self.harness,
            "model": self.model,
            "tier": self.tier,
            "seed": self.seed,
            "runs_per_task": self.runs_per_task,
            "max_steps": self.max_steps,
            "metrics": self.metrics.stable(),
            "tasks": [] if self.suite == "heldout" else [task.stable() for task in self.tasks],
            "budget": {
                "max_allowance_per_cycle": round(self.budget_suggestion.max_allowance_per_cycle, 9),
                "max_cycles_per_day": self.budget_suggestion.max_cycles_per_day,
                "reserve_weekly_pct": self.budget_suggestion.reserve_weekly_pct,
                "max_concurrency": self.budget_suggestion.max_concurrency,
                "cooldown_minutes": self.budget_suggestion.cooldown_minutes,
            },
            "validation_errors": list(self.validation_errors),
            "stopped_early": self.stopped_early,
        }


class EvalError(RuntimeError):
    pass


__all__ = [
    "BudgetSuggestion",
    "EvalError",
    "EvalMetrics",
    "EvalReport",
    "EvalTask",
    "EvalTaskResult",
    "MockAction",
    "MockScript",
    "Tier",
    "TrapSpec",
]
