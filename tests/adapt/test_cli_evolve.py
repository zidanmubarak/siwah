from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from siwah.cli.main import app


def test_evolve_seed_promotes_in_a_temp_harness(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SIWAH_CONFIG", raising=False)
    monkeypatch.delenv("SIWAH_EVOLVE", raising=False)
    runner = CliRunner()

    result = runner.invoke(app, ["evolve", "--seed"])
    assert result.exit_code == 0, result.output
    assert "promoted seed-verification-guidance" in result.output

    log = runner.invoke(app, ["evolve", "log"])
    assert log.exit_code == 0, log.output
    assert "evolve.promoted" in log.output

    rolled = runner.invoke(app, ["rollback", "evo-1"])
    assert rolled.exit_code == 0, rolled.output
    assert "harness rolled back to evo-1" in rolled.output

    paused = runner.invoke(app, ["evolve", "--pause"])
    assert paused.exit_code == 0
    assert "paused" in paused.output


def test_evolve_live_requires_opt_in_and_budget(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SIWAH_LIVE", raising=False)
    runner = CliRunner()

    missing_env = runner.invoke(app, ["evolve", "--live"])
    assert missing_env.exit_code == 2
    assert "SIWAH_LIVE=1" in missing_env.output
    assert "$env:SIWAH_LIVE" in missing_env.output

    monkeypatch.setenv("SIWAH_LIVE", "1")
    missing_budget = runner.invoke(app, ["evolve", "--live"])
    assert missing_budget.exit_code == 2
    assert "--max-allowance" in missing_budget.output


def test_evolve_rejects_an_environment_assignment_as_an_argument(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    result = CliRunner().invoke(app, ["evolve", "SIWAH_LIVE=1"])

    assert result.exit_code == 2
    assert "environment assignment" in result.output
    assert "$env:SIWAH_LIVE" in result.output


def test_evolve_reset_cooldown(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SIWAH_CONFIG", raising=False)

    result = CliRunner().invoke(app, ["evolve", "--reset-cooldown"])

    assert result.exit_code == 0, result.output
    assert "cooldown cleared" in result.output


def test_evolve_patch_file_guards(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    runner = CliRunner()

    missing = runner.invoke(app, ["evolve", "--patch-file", str(tmp_path / "missing.patch")])
    assert missing.exit_code == 2
    assert "Cannot read patch" in missing.output

    conflict = runner.invoke(
        app,
        ["evolve", "--patch-file", str(tmp_path / "missing.patch"), "--seed"],
    )
    assert conflict.exit_code == 2
    assert "cannot be combined" in conflict.output
