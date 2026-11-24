"""Compare two Siwah eval reports produced from the same replay fixtures."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from siwah.eval.models import EvalReport


def paired_results(before: EvalReport, after: EvalReport) -> dict:
    for field in ("suite", "model", "tier", "seed", "max_steps", "runs_per_task"):
        if getattr(before, field) != getattr(after, field):
            raise ValueError(f"Reports differ in {field}")
    if (
        before.stopped_early
        or after.stopped_early
        or before.validation_errors
        or after.validation_errors
    ):
        raise ValueError("Compare complete reports with no validation errors")
    left = {task.id: task for task in before.tasks}
    right = {task.id: task for task in after.tasks}
    if not left and not right:
        if before.suite != "heldout" or after.suite != "heldout":
            raise ValueError("Reports contain no task results")
        # The heldout suite intentionally omits per-task rows so private tasks
        # cannot leak. Compare the aggregate metrics instead.
        return {
            "suite": before.suite,
            "model": before.model,
            "counts": None,
            "tasks": [],
            "metrics": {
                "before": before.metrics.stable(),
                "after": after.metrics.stable(),
            },
        }
    if (
        not left
        or left.keys() != right.keys()
        or len(left) != len(before.tasks)
        or len(right) != len(after.tasks)
    ):
        raise ValueError("Reports must contain the same nonempty set of unique task IDs")
    counts = {"win": 0, "tie": 0, "loss": 0}
    rows = []
    for task_id in sorted(left):
        a, b = left[task_id], right[task_id]
        outcome = "tie" if a.passed == b.passed else ("win" if b.passed else "loss")
        counts[outcome] += 1
        rows.append(
            {
                "task": task_id,
                "outcome": outcome,
                "passed_before": a.passed,
                "passed_after": b.passed,
                "duration_delta_s": round(b.duration_s - a.duration_s, 3),
                "allowance_delta": round(b.allowance - a.allowance, 9),
            }
        )
    return {"model": before.model, "counts": counts, "tasks": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    args = parser.parse_args()
    try:
        result = paired_results(
            EvalReport.model_validate_json(args.before.read_bytes()),
            EvalReport.model_validate_json(args.after.read_bytes()),
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
