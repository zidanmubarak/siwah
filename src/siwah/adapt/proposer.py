"""Candidate proposal and validation."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

from ..kernel.candidate_safety import render_diff, validate_candidate
from .models import Candidate, Observation, Reflection

ModelCall = Callable[[str], Awaitable[str]]


class ProposalError(RuntimeError):
    pass


def parse_candidate(text: str) -> Candidate:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ProposalError("proposer did not return a JSON object")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ProposalError(f"proposer returned invalid JSON: {exc}") from exc
    try:
        return Candidate.model_validate(payload)
    except ValueError as exc:
        raise ProposalError(f"invalid candidate: {exc}") from exc


class LLMProposer:
    def __init__(self, call: ModelCall) -> None:
        self.call = call

    async def propose(self, reflection: Reflection, observations: list[Observation]) -> Candidate:
        prompt = (
            "Propose one Level 0 or Level 1 harness candidate as a JSON object with "
            "fields: id, level, kind, scope, target_paths, changes, rationale, "
            "evidence, expected_metric, risk, model. `changes` maps harness-relative "
            "paths to full new file contents (null deletes). Only edit prompts/, "
            "tool_guidance/, skills/, profiles/, or config.toml. Never edit the "
            "kernel, evals, permissions, or guardrails. Return JSON only.\n\n"
            f"Diagnosis:\n{reflection.summary}"
        )
        return parse_candidate(await self.call(prompt))


__all__ = [
    "LLMProposer",
    "ProposalError",
    "parse_candidate",
    "render_diff",
    "validate_candidate",
]
