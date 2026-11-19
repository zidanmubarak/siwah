"""Pydantic models shared by the evolution engine."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .eval_models import EvalMetrics

Scope = Literal["global", "project", "model"]
Level = Literal[0, 1, 2]


class Observation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_id: str
    model: str | None = None
    status: str = "unknown"
    user_messages: int = 0
    assistant_messages: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    corrections: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    allowance: float = 0.0
    duration_s: float = 0.0
    text: str = ""


class Reflection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summary: str
    signals: list[str] = Field(default_factory=list)
    model: str | None = None


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    level: Level
    kind: str
    scope: Scope
    target_paths: list[str]
    changes: dict[str, str | None]
    diff: str = ""
    rationale: str
    evidence: list[str] = Field(default_factory=list)
    expected_metric: str
    risk: str
    model: str | None = None


class ProbeResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tier: str
    golden: EvalMetrics
    heldout: EvalMetrics | None = None
    baseline: bool = False


class Promotion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    candidate_id: str
    tag: str
    commit: str
    changed_paths: list[str]
    previous_commit: str


class CanarySample(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_id: str
    tool_error_rate: float = 0.0
    correction_rate: float = 0.0
    abort_rate: float = 0.0
    allowance: float = 0.0


class CanaryDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ok: bool
    reasons: list[str] = Field(default_factory=list)
    sessions: int = 0


class EvolutionState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tag: str | None = None
    status: str = "idle"
    last_candidate: str | None = None
    last_gate: dict[str, Any] | None = None
    updated_at: str | None = None
    canary_tag: str | None = None
    canary_previous_commit: str | None = None
    canary_previous_tag: str | None = None
    canary_samples: list[dict[str, Any]] = Field(default_factory=list)


class EvolutionResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: str
    candidate: Candidate | None = None
    probe: ProbeResult | None = None
    gate: dict[str, Any] | None = None
    promotion: Promotion | None = None
    reason: str | None = None
    spent_allowance: float = 0.0
    duration_s: float = 0.0


__all__ = [
    "Candidate",
    "CanaryDecision",
    "CanarySample",
    "EvolutionResult",
    "EvolutionState",
    "Level",
    "Observation",
    "ProbeResult",
    "Promotion",
    "Reflection",
    "Scope",
]
