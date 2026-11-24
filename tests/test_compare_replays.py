from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from siwah.eval.models import BudgetSuggestion, EvalMetrics, EvalReport

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "compare_replays", ROOT / "scripts/compare-replays.py"
)
assert _spec is not None and _spec.loader is not None
compare_replays = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(compare_replays)


def _heldout(passed: int) -> EvalReport:
    return EvalReport(
        suite="heldout",
        harness="siwah",
        model="mock/model-a",
        tier="t1",
        seed=1,
        runs_per_task=1,
        max_steps=8,
        started_at="2026-11-24T00:00:00Z",
        duration_s=1.0,
        tasks=[],
        metrics=EvalMetrics(tasks=3, passed=passed, pass_rate=passed / 3),
        budget_suggestion=BudgetSuggestion(max_allowance_per_cycle=1.0),
    )


def test_heldout_reports_compare_aggregate_metrics():
    result = compare_replays.paired_results(_heldout(1), _heldout(2))
    assert result["suite"] == "heldout"
    assert result["tasks"] == []
    assert result["counts"] is None
    assert result["metrics"]["before"]["passed"] == 1
    assert result["metrics"]["after"]["passed"] == 2


def test_empty_non_heldout_reports_are_rejected():
    report = _heldout(1).model_copy(update={"suite": "golden"})
    with pytest.raises(ValueError, match="no task results"):
        compare_replays.paired_results(report, report)
