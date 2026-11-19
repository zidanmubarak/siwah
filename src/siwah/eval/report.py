"""JSON and table rendering for eval reports."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from rich import box
from rich.table import Table

from ..paths import siwah_home
from .models import EvalReport


def write_report(report: EvalReport, path: Path | str | None = None) -> Path:
    if path is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        path = siwah_home() / "eval" / "reports" / f"{stamp}-{report.suite}-{report.harness}.json"
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return target


def render_report(report: EvalReport, *, show_tasks: bool = True) -> Table:
    table = Table(
        title=(f"siwah eval · {report.suite} · {report.harness} · {report.tier} · {report.model}"),
        box=box.SIMPLE_HEAVY,
        header_style="bold",
    )
    table.add_column("task", overflow="ellipsis", no_wrap=True)
    table.add_column("cat")
    table.add_column("lang")
    table.add_column("pass", justify="center")
    table.add_column("steps", justify="right")
    table.add_column("tokens", justify="right")
    table.add_column("allow", justify="right")
    table.add_column("cache", justify="right")
    table.add_column("dur", justify="right")
    table.add_column("t-err", justify="right")
    table.add_column("g/t", justify="center")

    if show_tasks:
        for task in report.tasks:
            table.add_row(
                task.id.removeprefix("golden-").removeprefix("heldout-"),
                task.category,
                task.language,
                "PASS" if task.passed else "FAIL",
                str(task.steps),
                str(task.input_tokens + task.output_tokens),
                f"{task.allowance:.8f}",
                f"{task.cache_read_ratio * 100:.0f}%",
                f"{task.duration_s:.2f}s",
                f"{task.tool_errors}/{task.tool_calls}",
                ("guard" if task.guard_violation else "") + (" trap" if task.trap_triggered else "")
                or "-",
            )
    metrics = report.metrics
    table.add_section()
    table.add_row(
        f"TOTAL {metrics.passed}/{metrics.tasks}",
        f"{metrics.pass_rate * 100:.1f}%",
        "",
        "PASS" if metrics.passed == metrics.tasks else "FAIL",
        str(metrics.steps),
        str(metrics.input_tokens + metrics.output_tokens),
        f"{metrics.allowance:.8f}",
        f"{metrics.cache_read_ratio * 100:.0f}%",
        f"{metrics.duration_s:.2f}s",
        f"{metrics.tool_error_rate * 100:.1f}%",
        f"{metrics.guard_violations}/{metrics.trap_triggered}",
    )
    return table


def render_comparison(siwah: EvalReport, naive: EvalReport) -> Table:
    table = Table(
        title=f"siwah vs naive · {siwah.suite} · {siwah.model}",
        box=box.SIMPLE_HEAVY,
        header_style="bold",
    )
    table.add_column("harness")
    table.add_column("pass rate", justify="right")
    table.add_column("passed", justify="right")
    table.add_column("steps", justify="right")
    table.add_column("tokens", justify="right")
    table.add_column("allowance", justify="right")
    table.add_column("cache", justify="right")
    table.add_column("dur", justify="right")
    table.add_column("tool err", justify="right")
    for report in (siwah, naive):
        metrics = report.metrics
        table.add_row(
            report.harness,
            f"{metrics.pass_rate * 100:.1f}%",
            f"{metrics.passed}/{metrics.tasks}",
            str(metrics.steps),
            str(metrics.input_tokens + metrics.output_tokens),
            f"{metrics.allowance:.8f}",
            f"{metrics.cache_read_ratio * 100:.0f}%",
            f"{metrics.duration_s:.2f}s",
            f"{metrics.tool_error_rate * 100:.1f}%",
        )
    return table


def comparison_summary(siwah: EvalReport, naive: EvalReport) -> dict[str, object]:
    delta = siwah.metrics.pass_rate - naive.metrics.pass_rate
    return {
        "suite": siwah.suite,
        "model": siwah.model,
        "siwah_pass_rate": round(siwah.metrics.pass_rate, 6),
        "naive_pass_rate": round(naive.metrics.pass_rate, 6),
        "delta": round(delta, 6),
        "siwah_higher": delta > 0,
        "siwah_allowance": round(siwah.metrics.allowance, 9),
        "naive_allowance": round(naive.metrics.allowance, 9),
    }


__all__ = [
    "comparison_summary",
    "render_comparison",
    "render_report",
    "write_report",
]
