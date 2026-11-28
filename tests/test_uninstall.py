from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from siwah import uninstall
from siwah.cli.main import app
from siwah.tui_install import tui_binary_name


def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    (home / "harness").mkdir(parents=True)
    (home / "harness" / "config.toml").write_text("", encoding="utf-8")
    (home / "memory").mkdir()
    (home / "memory" / "user.md").write_text("private", encoding="utf-8")
    (home / "state.db").write_text("db", encoding="utf-8")
    (home / "bin").mkdir()
    (home / "bin" / tui_binary_name()).write_bytes(b"tui")
    (home / "runtime.json").write_text("{}", encoding="utf-8")
    (home / "update-check.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("SIWAH_HOME", str(home))
    return home


def test_keep_data_plan_removes_only_application_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = _home(tmp_path, monkeypatch)
    plan = uninstall.build_uninstall_plan(purge=False)
    assert plan.home == home.resolve()
    assert plan.data_paths == ()
    names = {path.name for path in plan.remove_now}
    assert tui_binary_name() in names
    assert "runtime.json" in names
    assert "update-check.json" in names
    assert (home / "memory" / "user.md").exists()


def test_purge_plan_lists_every_top_level_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = _home(tmp_path, monkeypatch)
    (home / "src" / ".git").mkdir(parents=True)
    plan = uninstall.build_uninstall_plan(purge=True)
    names = {path.name for path in plan.data_paths}
    assert names == {
        "bin",
        "harness",
        "memory",
        "runtime.json",
        "state.db",
        "src",
        "update-check.json",
    }

    keep = uninstall.build_uninstall_plan(purge=True, keep_source=True)
    assert "src" not in {path.name for path in keep.data_paths}
    assert plan.home == home.resolve()


def test_purge_refuses_an_unsafe_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SIWAH_HOME", str(Path.home()))
    with pytest.raises(uninstall.UninstallError, match="unsafe"):
        uninstall.build_uninstall_plan(purge=True)


def test_execute_purge_removes_data_and_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = _home(tmp_path, monkeypatch)
    monkeypatch.setattr(uninstall, "_stop_daemon", lambda: [])
    plan = uninstall.build_uninstall_plan(purge=True)
    plan = replace(plan, tool=uninstall.ToolRemoval(""))
    result = uninstall.execute_plan(plan)
    assert result.errors == ()
    assert not home.exists()
    assert any("state.db" in str(path) for path in result.removed)


def test_execute_keep_data_removes_tui_and_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = _home(tmp_path, monkeypatch)
    monkeypatch.setattr(uninstall, "_stop_daemon", lambda: [])
    plan = uninstall.build_uninstall_plan(purge=False)
    plan = replace(plan, tool=uninstall.ToolRemoval(""))
    result = uninstall.execute_plan(plan)
    assert result.errors == ()
    assert not (home / "bin" / tui_binary_name()).exists()
    assert not (home / "runtime.json").exists()
    assert (home / "memory" / "user.md").exists()


def test_tool_removal_prints_the_final_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = _home(tmp_path, monkeypatch)
    monkeypatch.setattr(uninstall, "_stop_daemon", lambda: [])
    plan = uninstall.build_uninstall_plan(purge=True)
    plan = replace(
        plan,
        tool=uninstall.ToolRemoval(
            label="uv",
            command=("uv", "tool", "uninstall", "siwah"),
        ),
    )
    result = uninstall.execute_plan(plan)
    assert result.errors == ()
    assert not home.exists()
    assert any("uv tool uninstall siwah" in note for note in result.notes)
    assert "run after this process exits" in "\n".join(uninstall.plan_lines(plan))


def test_uninstall_cli_dry_run_and_conflicts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _home(tmp_path, monkeypatch)
    runner = CliRunner()
    dry = runner.invoke(app, ["uninstall", "--dry-run", "--keep-data"])
    assert dry.exit_code == 0, dry.output
    assert "data:   keep" in dry.output
    assert tui_binary_name() in dry.output

    conflict = runner.invoke(app, ["uninstall", "--purge", "--keep-data"])
    assert conflict.exit_code == 2
    assert "cannot be combined" in conflict.output

    purge = runner.invoke(app, ["uninstall", "--purge", "--dry-run"])
    assert purge.exit_code == 0, purge.output
    assert "data:   remove" in purge.output


def test_uninstall_cli_purge_requires_yes_without_a_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _home(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(app, ["uninstall", "--purge"])
    assert result.exit_code == 2
    assert "--yes" in result.output
