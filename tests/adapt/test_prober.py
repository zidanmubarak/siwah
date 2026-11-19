from __future__ import annotations

from pathlib import Path

import pytest

from siwah.adapt.models import Candidate
from siwah.adapt.prober import EvalProber, ProbeError
from siwah.auth import AuthStore
from siwah.config import Config
from siwah.eval.models import BudgetSuggestion, EvalMetrics, EvalReport


def _report(allowance: float = 0.002) -> EvalReport:
    return EvalReport(
        suite="golden",
        harness="siwah",
        model="mock/model-a",
        tier="t1",
        seed=0,
        runs_per_task=1,
        max_steps=16,
        started_at="2026-11-09T00:00:00+00:00",
        duration_s=1.0,
        metrics=EvalMetrics(tasks=2, passed=2, pass_rate=1.0, allowance=allowance),
        budget_suggestion=BudgetSuggestion(max_allowance_per_cycle=0.1),
    )


@pytest.mark.asyncio
async def test_eval_prober_applies_candidate_to_a_temp_harness(tmp_path: Path):
    harness = tmp_path / "harness"
    (harness / "prompts").mkdir(parents=True)
    (harness / "prompts" / "system.md").write_text("baseline\n", encoding="utf-8")
    seen: list[tuple[str, str]] = []

    async def evaluate(home: Path, tier: str) -> EvalReport:
        seen.append((tier, (home / "harness" / "prompts" / "system.md").read_text()))
        return _report()

    prober = EvalProber(
        eval_dir=None,
        config=Config(),
        auth=AuthStore(tmp_path / "auth.json"),
        model="mock/model-a",
        evaluate=evaluate,
    )
    candidate = Candidate(
        id="candidate",
        level=1,
        kind="prompt",
        scope="global",
        target_paths=["prompts/system.md"],
        changes={"prompts/system.md": "candidate\n"},
        rationale="test",
        expected_metric="pass_rate_up",
        risk="low",
    )

    baseline = await prober.baseline(harness)
    result = await prober.probe(candidate, harness, tier="t2")

    assert baseline.golden.pass_rate == 1.0
    assert result.golden.pass_rate == 1.0
    assert seen == [("t2", "baseline\n"), ("t2", "candidate\n")]
    assert prober.spent_allowance == pytest.approx(0.004)


@pytest.mark.asyncio
async def test_eval_prober_rejects_paths_outside_the_harness(tmp_path: Path):
    harness = tmp_path / "harness"
    harness.mkdir()

    async def evaluate(_home: Path, _tier: str) -> EvalReport:
        return _report()

    prober = EvalProber(
        eval_dir=None,
        config=Config(),
        auth=AuthStore(tmp_path / "auth.json"),
        model="mock/model-a",
        evaluate=evaluate,
    )
    candidate = Candidate(
        id="candidate",
        level=1,
        kind="prompt",
        scope="global",
        target_paths=["../outside.md"],
        changes={"../outside.md": "bad"},
        rationale="test",
        expected_metric="pass_rate_up",
        risk="low",
    )
    with pytest.raises(ProbeError, match="outside the harness"):
        await prober.probe(candidate, harness)
