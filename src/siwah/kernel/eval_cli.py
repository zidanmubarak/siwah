"""Protected Level 2 evaluation entry; never trust the mutable public CLI verdict."""

from __future__ import annotations

import argparse
import asyncio
import json
import os

from .evaluation import EvalRunner
from .gate import evaluate_gate
from .policy import GateConfig


async def _evaluate(tier: str, baseline_source: str, policy: GateConfig) -> tuple[bool, dict]:
    # Resolve the protected eval tree from the installed package. The heldout
    # suite can be redirected with SIWAH_HELDOUT_DIR for private benchmarks.
    runner = EvalRunner()
    if tier == "t0":
        reports = [
            await runner.run(suite=suite, tier="t0", use_cache=False)
            for suite in ("golden", "heldout", "traps")
        ]
        errors = [error for report in reports for error in report.validation_errors]
        return not errors, {"validation_errors": errors}
    candidate_source = os.environ.get("SIWAH_SUBJECT_SOURCE")
    reports = {}
    suites = ("golden", "heldout") if tier == "t3" else ("golden",)
    try:
        for suite in suites:
            os.environ["SIWAH_SUBJECT_SOURCE"] = baseline_source
            baseline = await runner.run(
                suite=suite,
                tier=tier,
                use_cache=False,
                runs_per_task=policy.runs_per_task,
            )
            if candidate_source is None:
                os.environ.pop("SIWAH_SUBJECT_SOURCE", None)
            else:
                os.environ["SIWAH_SUBJECT_SOURCE"] = candidate_source
            candidate = await runner.run(
                suite=suite,
                tier=tier,
                use_cache=False,
                runs_per_task=policy.runs_per_task,
            )
            reports[suite] = (baseline, candidate)
    finally:
        if candidate_source is None:
            os.environ.pop("SIWAH_SUBJECT_SOURCE", None)
        else:
            os.environ["SIWAH_SUBJECT_SOURCE"] = candidate_source
    base, candidate = reports["golden"]
    valid = all(
        not report.validation_errors and not report.stopped_early and report.metrics.tasks > 0
        for pair in reports.values()
        for report in pair
    )
    if tier != "t3":
        return valid and candidate.metrics.pass_rate >= base.metrics.pass_rate, {
            "baseline": base.metrics.model_dump(),
            "candidate": candidate.metrics.model_dump(),
        }
    heldout_base, heldout_candidate = reports["heldout"]
    gate = evaluate_gate(
        base.metrics,
        candidate.metrics,
        policy,
        heldout_baseline=heldout_base.metrics,
        heldout_candidate=heldout_candidate.metrics,
    )
    return valid and gate.accepted, {"gate": gate.__dict__}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tier", required=True, choices=("t0", "t1", "t2", "t3"))
    parser.add_argument("--suite", default="golden", choices=("golden",))
    parser.add_argument("--baseline-source", required=True)
    parser.add_argument("--policy", required=True)
    args = parser.parse_args()
    ok, summary = asyncio.run(
        _evaluate(
            args.tier,
            args.baseline_source,
            GateConfig.model_validate_json(args.policy),
        )
    )
    print(json.dumps(summary))
    raise SystemExit(0 if ok else 1)
