from __future__ import annotations

import json

import pytest

from siwah.adapt.models import Reflection
from siwah.adapt.proposer import (
    CandidateSchemaError,
    LLMProposer,
    ProposalError,
    parse_candidate,
)


def _payload(**overrides):
    payload = {
        "id": "candidate-1",
        "level": 1,
        "kind": "system_prompt",
        "scope": "global",
        "target_paths": ["prompts/system.md"],
        "changes": {"prompts/system.md": "updated"},
        "rationale": "A concise improvement.",
        "evidence": ["observed correction"],
        "expected_metric": "pass_rate_up",
        "risk": "low",
        "model": None,
    }
    payload.update(overrides)
    return payload


def test_parse_candidate_normalizes_common_model_drift():
    candidate = parse_candidate(
        json.dumps(
            _payload(
                scope="agent",
                level="1",
                target_paths="prompts/system.md",
                evidence="the user corrected the same mistake",
            )
        )
    )

    assert candidate.scope == "global"
    assert candidate.level == 1
    assert candidate.target_paths == ["prompts/system.md"]
    assert candidate.evidence == ["the user corrected the same mistake"]


def test_parse_candidate_reports_schema_errors_compactly():
    payload = _payload()
    del payload["changes"]

    with pytest.raises(CandidateSchemaError) as error:
        parse_candidate(json.dumps(payload))

    assert "changes" in str(error.value)
    assert "invalid candidate" in str(error.value)


def test_parse_candidate_rejects_non_json_without_a_schema_retry():
    with pytest.raises(ProposalError) as error:
        parse_candidate("I could not produce a candidate.")

    assert not isinstance(error.value, CandidateSchemaError)


async def test_llm_proposer_repairs_one_schema_error():
    invalid = _payload()
    del invalid["changes"]
    responses = iter([json.dumps(invalid), json.dumps(_payload())])
    prompts: list[str] = []

    async def call(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    proposer = LLMProposer(call)
    candidate = await proposer.propose(Reflection(summary="needs a small fix"), [])

    assert candidate.id == "candidate-1"
    assert len(prompts) == 2
    assert "changes" in prompts[1]


async def test_llm_proposer_does_not_retry_a_non_json_answer():
    calls = 0

    async def call(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return "not json"

    proposer = LLMProposer(call)
    with pytest.raises(ProposalError):
        await proposer.propose(Reflection(summary="needs a small fix"), [])

    assert calls == 1
