"""Candidate proposal, normalization, and validation."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any, cast

from ..kernel.candidate_safety import render_diff, validate_candidate
from .models import Candidate, Observation, Reflection

ModelCall = Callable[[str], Awaitable[str]]


class ProposalError(RuntimeError):
    pass


class CandidateSchemaError(ProposalError):
    """The model returned JSON, but it did not match the candidate schema."""


_SCOPE_ALIASES = {
    "agent": "global",
    "harness": "global",
    "global": "global",
    "project": "project",
    "repo": "project",
    "repository": "project",
    "workspace": "project",
    "model": "model",
}

_SCHEMA = """{
  "id": "short-stable-id",
  "level": 0,
  "kind": "system_prompt",
  "scope": "global",
  "target_paths": ["prompts/system.md"],
  "changes": {"prompts/system.md": "full new file content"},
  "diff": "",
  "rationale": "why this change should help",
  "evidence": ["one item per concrete observation"],
  "expected_metric": "pass_rate_up",
  "risk": "low; explain the blast radius",
  "model": null
}"""


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [
            item if isinstance(item, str) else json.dumps(item, ensure_ascii=False, sort_keys=True)
            for item in value
        ]
    return [json.dumps(value, ensure_ascii=False, sort_keys=True)]


def _normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Coerce common model drift into the strict Candidate schema.

    The proposer is an LLM. A schema mistake should not waste a probe cycle if
    the intent is unambiguous: ``scope: "agent"`` means the global harness,
    ``level: "1"`` is level 1, and a single evidence sentence is a one-item
    list. Anything genuinely ambiguous still fails validation.
    """

    normalized = dict(payload)
    scope = normalized.get("scope")
    if isinstance(scope, str):
        normalized["scope"] = _SCOPE_ALIASES.get(scope.strip().lower(), scope)

    level = normalized.get("level")
    if isinstance(level, str):
        match = re.fullmatch(r"(?:level\s*)?([012])", level.strip().lower())
        if match is not None:
            normalized["level"] = int(match.group(1))

    for key in ("target_paths", "evidence"):
        if key in normalized:
            normalized[key] = _string_list(normalized[key])

    if normalized.get("model") in {"", "null", "none", "None"}:
        normalized["model"] = None
    return normalized


def _validation_message(exc: ValueError) -> str:
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return str(exc)
    parts: list[str] = []
    for error in cast(list[dict[str, Any]], errors()):
        location = ".".join(str(part) for part in error.get("loc", ()))
        message = str(error.get("msg", exc))
        parts.append(f"{location}: {message}" if location else message)
    return "; ".join(parts) or str(exc)


def parse_candidate(text: str) -> Candidate:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ProposalError("proposer did not return a JSON object")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ProposalError(f"proposer returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProposalError("proposer JSON must be an object")
    try:
        return Candidate.model_validate(_normalize_payload(payload))
    except ValueError as exc:
        raise CandidateSchemaError(f"invalid candidate: {_validation_message(exc)}") from exc


class LLMProposer:
    def __init__(self, call: ModelCall) -> None:
        self.call = call

    async def propose(self, reflection: Reflection, observations: list[Observation]) -> Candidate:
        prompt = (
            "Propose one Level 0 or Level 1 harness candidate as a JSON object. "
            "Use exactly this shape:\n"
            f"{_SCHEMA}\n\n"
            "Rules: `scope` must be global, project, or model. `evidence` must be "
            "a list of strings. `changes` maps harness-relative paths to full new "
            "file contents (null deletes). Only edit prompts/, tool_guidance/, "
            "skills/, profiles/, or config.toml. Never edit the kernel, evals, "
            "permissions, or guardrails. Return JSON only.\n\n"
            f"Diagnosis:\n{reflection.summary}"
        )
        text = await self.call(prompt)
        try:
            return parse_candidate(text)
        except CandidateSchemaError as exc:
            repaired = await self.call(
                "Your previous response was valid JSON but did not match the "
                "candidate schema.\n"
                f"Validation errors: {exc}\n\n"
                "Return one corrected JSON object only, with exactly this shape:\n"
                f"{_SCHEMA}\n\n"
                "Do not add fields outside the schema. Return JSON only."
            )
            return parse_candidate(repaired)


__all__ = [
    "CandidateSchemaError",
    "LLMProposer",
    "ProposalError",
    "parse_candidate",
    "render_diff",
    "validate_candidate",
]
