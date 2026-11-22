from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from siwah.cli.main import app
from siwah.mcp.client import load_mcp_config

from .test_plugins import _make_plugin


def test_plugin_cli_install_list_info_disable_remove(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SIWAH_CONFIG", raising=False)
    source = _make_plugin(tmp_path / "source")
    runner = CliRunner()

    installed = runner.invoke(app, ["plugin", "install", str(source)])
    assert installed.exit_code == 0, installed.output
    assert "Installed demo" in installed.output

    listed = runner.invoke(app, ["plugin", "list"])
    assert listed.exit_code == 0, listed.output
    assert "demo" in listed.output
    assert "enabled" in listed.output

    info = runner.invoke(app, ["plugin", "info", "demo"])
    assert info.exit_code == 0, info.output
    assert "skills=1" in info.output
    assert "mcp_servers=1" in info.output

    disabled = runner.invoke(app, ["plugin", "disable", "demo"])
    assert disabled.exit_code == 0, disabled.output
    assert "Disabled plugin demo" in disabled.output

    removed = runner.invoke(app, ["plugin", "remove", "demo", "--yes"])
    assert removed.exit_code == 0, removed.output
    assert "Removed plugin demo" in removed.output


def test_mcp_cli_adds_oauth_server(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SIWAH_CONFIG", raising=False)
    runner = CliRunner()

    added = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "context7",
            "--url",
            "https://mcp.example.com/mcp",
            "--auth",
            "oauth",
            "--scope",
            "profile",
            "--scope",
            "email",
        ],
    )
    assert added.exit_code == 0, added.output
    server = load_mcp_config()["servers"]["context7"]
    assert server["auth"] == "oauth"
    assert server["scopes"] == ["profile", "email"]

    listed = runner.invoke(app, ["mcp", "list"])
    assert listed.exit_code == 0, listed.output
    assert "context7" in listed.output
    assert "oauth" in listed.output


def test_mcp_cli_rejects_oauth_with_a_static_authorization_header(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SIWAH_CONFIG", raising=False)
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "bad",
            "--url",
            "https://mcp.example.com/mcp",
            "--auth",
            "oauth",
            "--header",
            "Authorization=Bearer old",
        ],
    )

    assert result.exit_code == 2
    assert "static Authorization" in result.output
    assert "bad" not in load_mcp_config()["servers"]
