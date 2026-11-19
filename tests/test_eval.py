"""Evaluation engine tests: fixtures, runner, guards, cache, and budget."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from siwah.cli.main import app
from siwah.config import BudgetConfig, default_config_text, load_config, update_budget_config
from siwah.eval.mock import basic_tools
from siwah.eval.models import EvalError, EvalMetrics, EvalTaskResult, Tier
from siwah.eval.report import comparison_summary, render_comparison, render_report, write_report
from siwah.eval.runner import EvalRunner, _budget, _cache_key
from siwah.eval.tasks import load_tasks, select_tier, task_statistics, validate_suite

SUITES = ("golden", "heldout", "traps")
ROOT = Path(__file__).resolve().parents[1]


def _write_task(root: Path, suite: str, payload: dict) -> None:
    directory = root / suite
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{payload['id']}.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


def test_committed_fixtures_cover_the_required_shape():
    golden = load_tasks("golden")
    heldout = load_tasks("heldout")
    traps = load_tasks("traps")

    golden_stats = task_statistics(golden)
    assert cast(int, golden_stats["count"]) >= 30
    assert set(cast(dict[str, int], golden_stats["languages"])) == {"python", "typescript"}
    assert set(cast(dict[str, int], golden_stats["difficulties"])) == {"easy", "medium", "hard"}
    required_categories = {
        "bugfix",
        "feature",
        "refactor",
        "tests",
        "multi-file",
        "comprehension",
        "tool-hygiene",
    }
    assert required_categories <= set(cast(dict[str, int], golden_stats["categories"]))

    # The public repository ships three representative heldout samples; the
    # full private suite is loaded from SIWAH_HELDOUT_DIR. The private release
    # gate keeps the ten-task minimum.
    assert len(heldout) >= 3
    assert len(traps) >= 3
    assert {task.trap.kind for task in traps if task.trap} == {
        "reward_hacking",
        "prompt_injection",
        "destructive",
    }

    ids = [task.id for task in [*golden, *heldout, *traps]]
    assert len(ids) == len(set(ids))
    for suite in SUITES:
        assert validate_suite(suite) == []


def test_heldout_suite_can_load_from_an_external_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    payload = json.loads(
        (ROOT / "evals/heldout/heldout-bugfix-01.json").read_text(encoding="utf-8")
    )
    _write_task(tmp_path, "heldout", payload)
    monkeypatch.setenv("SIWAH_HELDOUT_DIR", str(tmp_path / "heldout"))

    tasks = load_tasks("heldout")

    assert [task.id for task in tasks] == ["heldout-bugfix-01"]


def test_explicit_eval_root_wins_over_the_heldout_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    payload = json.loads(
        (ROOT / "evals/heldout/heldout-bugfix-01.json").read_text(encoding="utf-8")
    )
    _write_task(tmp_path, "heldout", payload)
    monkeypatch.setenv("SIWAH_HELDOUT_DIR", str(tmp_path / "elsewhere"))

    tasks = load_tasks("heldout", tmp_path)

    assert [task.id for task in tasks] == ["heldout-bugfix-01"]


def test_t0_validation_reports_errors_without_running(tmp_path: Path):
    _write_task(
        tmp_path,
        "golden",
        {
            "id": "broken-task",
            "category": "bugfix",
            "language": "python",
            "difficulty": "easy",
            "prompt": "Do something.",
        },
    )
    runner = EvalRunner(eval_dir=tmp_path, cache_dir=tmp_path / "cache")

    report = _run(runner, tier=Tier.T0)

    assert report.tasks == []
    assert report.metrics.tasks == 0
    assert report.validation_errors
    assert "check or check_text is required" in report.validation_errors[0]


def test_loader_rejects_unsafe_paths_and_secret_values(tmp_path: Path):
    unsafe = {
        "id": "unsafe-task",
        "category": "bugfix",
        "language": "python",
        "difficulty": "easy",
        "prompt": "Do something.",
        "files": {"../escape.py": "print('x')\n"},
        "check_text": "x",
    }
    _write_task(tmp_path, "golden", unsafe)
    with pytest.raises(EvalError, match="unsafe file path"):
        load_tasks("golden", tmp_path)

    secret_root = tmp_path / "secret"
    secret = {
        "id": "secret-task",
        "category": "bugfix",
        "language": "python",
        "difficulty": "easy",
        "prompt": "Do something.",
        "files": {"config.py": "API_KEY = 'sk-abcdefghijklmnopqrstuvwxyz012345'\n"},
        "check_text": "x",
    }
    _write_task(secret_root, "golden", secret)
    with pytest.raises(EvalError, match="credential-like value"):
        load_tasks("golden", secret_root)


def test_tier_selection_is_bounded():
    tasks = load_tasks("golden")
    assert len(select_tier(tasks, Tier.T1)) == 6
    t2 = select_tier(tasks, Tier.T2)
    assert 0 < len(t2) <= 12
    assert all(task.category != "trap" for task in t2)
    assert len(select_tier(tasks, Tier.T3)) == len(tasks)
    assert len(select_tier(tasks, Tier.T3, limit=4)) == 4


def test_two_mock_runs_produce_identical_scores(tmp_path: Path):
    runner = EvalRunner(cache_dir=tmp_path / "cache")
    selected = ["golden-bugfix-01", "golden-verifier-01", "golden-tests-01"]

    first = _run(
        runner,
        tier=Tier.T3,
        only=selected,
        use_cache=False,
    )
    second = _run(
        runner,
        tier=Tier.T3,
        only=selected,
        use_cache=False,
    )

    assert first.metrics.passed == first.metrics.tasks == 3
    assert first.stable_projection() == second.stable_projection()


def test_report_contains_all_required_metrics(tmp_path: Path):
    runner = EvalRunner(cache_dir=tmp_path / "cache")
    report = _run(
        runner,
        tier=Tier.T3,
        only=["golden-bugfix-01"],
        use_cache=False,
    )
    metrics = report.metrics

    assert metrics.tasks == 1
    assert metrics.passed == 1
    assert metrics.pass_rate == 1.0
    assert metrics.steps >= 2
    assert metrics.input_tokens > 0
    assert metrics.output_tokens > 0
    assert metrics.allowance > 0
    assert metrics.duration_s > 0
    assert metrics.tool_calls >= 1
    assert metrics.tool_errors == 0
    assert metrics.tool_error_rate == 0.0
    assert metrics.cache_read_tokens > 0
    assert 0 < metrics.cache_read_ratio <= 1
    assert report.max_steps == 8

    task = report.tasks[0]
    assert task.check_ok and task.check_text_ok
    assert not task.guard_violation and not task.trap_triggered
    assert task.steps >= 2 and task.tool_calls >= 1

    target = write_report(report, tmp_path / "report.json")
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["metrics"]["pass_rate"] == 1.0
    assert payload["metrics"]["tool_error_rate"] == 0.0
    assert payload["metrics"]["cache_read_ratio"] > 0
    assert payload["metrics"]["duration_s"] > 0
    assert payload["budget_suggestion"]["max_allowance_per_cycle"] > 0
    assert payload["tasks"][0]["id"] == "golden-bugfix-01"
    rendered = render_report(report)
    headers = {str(column.header) for column in rendered.columns}
    assert {"pass", "steps", "tokens", "allow", "cache", "dur", "t-err"} <= headers
    assert rendered.row_count > 0


def test_repeated_runs_sum_cost_and_tokens(tmp_path: Path):
    runner = EvalRunner(cache_dir=tmp_path / "cache")
    report = _run(
        runner,
        tier=Tier.T3,
        only=["golden-bugfix-01"],
        use_cache=False,
        runs_per_task=2,
    )

    task = report.tasks[0]
    assert task.passed is True
    assert task.steps == 4
    assert task.input_tokens == 4_000
    assert task.output_tokens == 200
    assert task.allowance == pytest.approx(0.0004)
    assert task.tool_calls == 2
    assert report.metrics.steps == 4
    assert report.metrics.allowance == pytest.approx(0.0004)
    assert report.metrics.cache_read_ratio == pytest.approx(0.8)


def test_max_steps_is_recorded_and_changes_the_cache_key(tmp_path: Path):
    task = load_tasks("golden")[0]
    assert _cache_key(task, "siwah", "mock/model-a", 0, 0, 8) != _cache_key(
        task, "siwah", "mock/model-a", 0, 0, 16
    )

    report = _run(
        EvalRunner(cache_dir=tmp_path / "cache"),
        tier=Tier.T3,
        only=[task.id],
        use_cache=False,
        max_steps=9,
    )
    assert report.max_steps == 9
    assert report.stable_projection()["max_steps"] == 9
    assert report.metrics.passed == 1


def test_siwah_scores_higher_than_naive_on_verifier_tasks(tmp_path: Path):
    runner = EvalRunner(cache_dir=tmp_path / "cache")
    selected = ["golden-verifier-01", "golden-verifier-02"]

    siwah = _run(runner, tier=Tier.T3, only=selected, use_cache=False)
    naive = _run(
        runner,
        harness="naive",
        tier=Tier.T3,
        only=selected,
        use_cache=False,
    )

    assert siwah.metrics.passed == 2
    assert naive.metrics.passed < siwah.metrics.passed
    summary = comparison_summary(siwah, naive)
    assert summary["siwah_higher"] is True
    assert cast(float, summary["delta"]) > 0
    assert render_comparison(siwah, naive).row_count == 2


def test_baseline_cache_reuses_results(tmp_path: Path):
    runner = EvalRunner(cache_dir=tmp_path / "cache")

    first = _run(runner, tier=Tier.T3, only=["golden-bugfix-01"])
    second = _run(runner, tier=Tier.T3, only=["golden-bugfix-01"])

    assert first.cache_hits == 0
    assert second.cache_hits == 1
    assert first.metrics.stable() == second.metrics.stable()
    assert list((tmp_path / "cache").glob("*.json"))

    runner._save_cache(
        "error-result",
        EvalTaskResult(
            id="error-task",
            category="bugfix",
            language="python",
            difficulty="easy",
            error="boom",
        ),
    )
    assert not (tmp_path / "cache" / "error-result.json").exists()


def test_traps_are_detected_by_guards_and_markers(tmp_path: Path):
    runner = EvalRunner(cache_dir=tmp_path / "cache")
    report = _run(runner, suite="traps", tier=Tier.T3, use_cache=False)
    by_id = {task.id: task for task in report.tasks}

    reward = by_id["trap-reward-hacking"]
    assert reward.guard_violation is True
    assert reward.trap_triggered is True
    assert reward.passed is False

    injection = by_id["trap-prompt-injection"]
    assert injection.trap_triggered is True
    assert injection.passed is False

    destructive = by_id["trap-destructive"]
    assert destructive.trap_triggered is False
    assert destructive.passed is True
    assert destructive.tool_errors >= 1
    assert report.metrics.guard_violations == 1
    assert report.metrics.trap_triggered == 2


def test_heldout_report_exposes_only_aggregates(tmp_path: Path):
    runner = EvalRunner(cache_dir=tmp_path / "cache")
    report = _run(
        runner,
        suite="heldout",
        tier=Tier.T3,
        only=["heldout-bugfix-01"],
        use_cache=False,
    )

    assert report.tasks == []
    assert report.metrics.tasks == 1
    assert report.metrics.passed == 1
    target = write_report(report, tmp_path / "heldout.json")
    text = target.read_text(encoding="utf-8")
    assert json.loads(text)["tasks"] == []
    assert "heldout-bugfix-01" not in text


def test_live_eval_requires_opt_in_budget_and_stops_at_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    runner = EvalRunner(cache_dir=tmp_path / "cache")
    monkeypatch.delenv("SIWAH_LIVE", raising=False)
    with pytest.raises(EvalError, match="SIWAH_LIVE=1"):
        _run(runner, live=True)

    monkeypatch.setenv("SIWAH_LIVE", "1")
    with pytest.raises(EvalError, match="max_allowance"):
        _run(runner, live=True)

    async def fake_run(task, harness, model, *, live, max_steps):
        return EvalTaskResult(
            id=task.id,
            category=task.category,
            language=task.language,
            difficulty=task.difficulty,
            passed=True,
            steps=1,
            allowance=0.6,
        )

    monkeypatch.setattr(runner, "_run_task", fake_run)
    report = _run(
        runner,
        tier=Tier.T3,
        only=["golden-bugfix-01", "golden-bugfix-02"],
        live=True,
        max_allowance=0.5,
    )
    assert report.metrics.tasks == 1
    assert report.metrics.allowance == 0.6
    assert report.stopped_early is True


def test_budget_defaults_and_calibration(tmp_path: Path):
    assert "[budget]" in default_config_text()
    assert BudgetConfig().max_wall_minutes == 45

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "# keep this comment\n[roles]\nmain = 'mock/model-a'\n\n"
        "[budget] # calibrated\nmax_cycles_per_day = 9\nmax_wall_minutes = 99\n",
        encoding="utf-8",
    )

    target = update_budget_config(
        {
            "max_allowance_per_cycle": 0.25,
            "max_cycles_per_day": 3,
            "reserve_weekly_pct": 40,
            "max_concurrency": 2,
            "cooldown_minutes": 60,
            "max_wall_minutes": 7,
        },
        path=config_path,
    )
    assert target == config_path
    text = config_path.read_text(encoding="utf-8")
    assert "# keep this comment" in text
    assert text.count("[budget]") == 1

    config = load_config(config_path)
    assert isinstance(config.budget, BudgetConfig)
    assert config.budget.max_allowance_per_cycle == 0.25
    assert config.budget.max_cycles_per_day == 3
    assert config.budget.max_wall_minutes == 7

    suggestion = _budget(EvalMetrics(allowance=0.2, duration_s=100))
    assert suggestion.max_allowance_per_cycle == 0.3
    assert suggestion.max_wall_minutes == 3


def test_naive_baseline_uses_only_basic_tools():
    names = {tool.name for tool in basic_tools()}
    assert names == {"read_file", "write_file", "edit_file", "glob", "grep", "bash", "todo"}
    assert "repo_map" not in names
    assert "verify" not in names


def test_cli_validates_and_compares(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SIWAH_CONFIG", raising=False)
    runner = CliRunner()

    validated = runner.invoke(app, ["eval", "--suite", "golden", "--tier", "t0", "--json"])
    assert validated.exit_code == 0, validated.output
    payload = json.loads(validated.stdout)
    assert payload["siwah"]["validation_errors"] == []
    assert payload["siwah"]["metrics"]["tasks"] == 0

    compared = runner.invoke(
        app,
        [
            "eval",
            "--suite",
            "golden",
            "--only",
            "golden-verifier-01",
            "--baseline",
            "--no-cache",
            "--json",
        ],
    )
    assert compared.exit_code == 0, compared.output
    payload = json.loads(compared.stdout)
    assert payload["comparison"]["siwah_higher"] is True
    assert payload["comparison"]["delta"] > 0
    assert payload["siwah"]["metrics"]["passed"] == 1
    assert payload["naive"]["metrics"]["passed"] == 0

    no_measurement = runner.invoke(app, ["eval", "--write-budget"])
    assert no_measurement.exit_code == 2
    assert "--write-budget requires --live" in no_measurement.output

    conflict = runner.invoke(app, ["eval", "--harness", "naive", "--baseline"])
    assert conflict.exit_code == 2
    assert "--baseline compares siwah with naive" in conflict.output


def _run(
    runner: EvalRunner,
    *,
    suite: str = "golden",
    harness: str = "siwah",
    tier: Tier = Tier.T3,
    only: list[str] | None = None,
    use_cache: bool = True,
    runs_per_task: int = 1,
    max_steps: int = 8,
    live: bool = False,
    max_allowance: float | None = None,
):
    return asyncio.run(
        runner.run(
            suite=suite,
            harness=harness,
            tier=tier,
            only=only,
            use_cache=use_cache,
            runs_per_task=runs_per_task,
            max_steps=max_steps,
            live=live,
            max_allowance=max_allowance,
        )
    )
