from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer import Exit
from typer.testing import CliRunner

from siwah.cli import main as cli_main
from siwah.cli.main import app
from siwah.cli.plain_ui import PlainRenderer
from siwah.config import AuthConfig, Config, ProviderConfig, RolesConfig
from siwah.core import loop as agent_loop
from siwah.version import __version__


def test_version_and_openapi():
    runner = CliRunner()
    version = runner.invoke(app, ["--version"])
    assert version.exit_code == 0
    assert f"siwah {__version__}" in version.stdout

    schema = runner.invoke(app, ["openapi"])
    assert schema.exit_code == 0
    parsed = json.loads(schema.stdout)
    enum = parsed["components"]["schemas"]["AuthProviderStatus"]["properties"]["browser_method"][
        "anyOf"
    ][0]["enum"]
    assert enum == sorted(enum)
    parsed = json.loads(schema.stdout)
    assert parsed["openapi"].startswith("3.")
    assert parsed["info"]["version"] == __version__
    assert "/v1/sessions" in parsed["paths"]


def test_pick_free_port_avoids_busy_preferred_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        preferred = int(busy.getsockname()[1])
        port, fallback = cli_main._pick_free_port("127.0.0.1", preferred)
        assert fallback is True
        assert port != preferred
    port, fallback = cli_main._pick_free_port("127.0.0.1", 0)
    assert fallback is False
    assert port > 0


def test_find_tui_binary_prefers_explicit_path(tmp_path: Path, monkeypatch):
    binary = tmp_path / "custom-tui.exe"
    binary.write_text("binary", encoding="utf-8")
    monkeypatch.setenv("SIWAH_TUI_BINARY", str(binary))
    assert cli_main._find_tui_binary() == binary


def test_find_tui_binary_uses_siwah_home_bin(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("SIWAH_TUI_BINARY", raising=False)
    home = tmp_path / "home"
    bin_dir = home / "bin"
    bin_dir.mkdir(parents=True)
    binary = bin_dir / ("siwah-tui.exe" if os.name == "nt" else "siwah-tui")
    binary.write_text("binary", encoding="utf-8")
    monkeypatch.setenv("SIWAH_HOME", str(home))
    assert cli_main._find_tui_binary() == binary


def test_promote_tui_binary_activates_a_staged_build(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    bin_dir = home / "bin"
    bin_dir.mkdir(parents=True)
    name = cli_main._tui_binary_name()
    target = bin_dir / name
    staged = bin_dir / f"{name}.next"
    target.write_text("old", encoding="utf-8")
    staged.write_text("new", encoding="utf-8")
    monkeypatch.setenv("SIWAH_HOME", str(home))

    cli_main._promote_tui_binary()

    assert target.read_text(encoding="utf-8") == "new"
    assert not staged.exists()


def test_launch_uses_staged_binary_on_first_launch(tmp_path: Path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    name = cli_main._tui_binary_name()
    staged = bin_dir / f"{name}.next"
    staged.write_bytes(b"new")
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path))
    monkeypatch.delenv("SIWAH_TUI_BINARY", raising=False)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli_main, "_ensure_daemon", lambda: ("server", "token", None))
    monkeypatch.setattr(cli_main, "_find_bun", lambda: None)

    def run(command, **_kwargs):
        assert command == [str(bin_dir / name)]
        assert (bin_dir / name).read_bytes() == b"new"
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(cli_main.subprocess, "run", run)
    with pytest.raises(Exit) as result:
        cli_main.launch_tui()
    assert result.value.exit_code == 0


def test_launch_without_tui_or_bun_prints_bootstrap_hint(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path))
    monkeypatch.delenv("SIWAH_TUI_BINARY", raising=False)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setenv("SIWAH_TUI_AUTO_DOWNLOAD", "0")
    monkeypatch.setattr(cli_main, "_promote_tui_binary", lambda: None)
    monkeypatch.setattr(cli_main, "_find_tui_binary", lambda: None)
    monkeypatch.setattr(cli_main, "_find_bun", lambda: None)
    monkeypatch.setattr(cli_main, "_repo_tui_dir", lambda: tmp_path / "missing")

    with pytest.raises(Exit) as result:
        cli_main.launch_tui()
    assert result.value.exit_code == 1
    assert "bootstrap.sh" in capsys.readouterr().err


def test_launch_downloads_the_tui_when_none_is_available(tmp_path: Path, monkeypatch):
    target = tmp_path / "bin" / "siwah-tui"
    target.parent.mkdir()
    target.write_bytes(b"tui")
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path))
    monkeypatch.delenv("SIWAH_TUI_BINARY", raising=False)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli_main, "_promote_tui_binary", lambda: None)
    monkeypatch.setattr(cli_main, "_find_tui_binary", lambda: None)
    monkeypatch.setattr(cli_main, "_find_bun", lambda: None)
    monkeypatch.setattr(cli_main, "_repo_tui_dir", lambda: tmp_path / "missing")
    monkeypatch.setattr(cli_main, "ensure_tui_binary", lambda: target)
    monkeypatch.setattr(cli_main, "_ensure_daemon", lambda: ("server", "token", None))
    monkeypatch.setattr(cli_main.typer, "echo", lambda *_args, **_kwargs: None)

    def run(command, **_kwargs):
        assert command == [str(target)]
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(cli_main.subprocess, "run", run)
    with pytest.raises(Exit) as result:
        cli_main.launch_tui()
    assert result.value.exit_code == 0


def test_install_tui_command_reports_the_target(tmp_path: Path, monkeypatch):
    target = tmp_path / "home" / "bin" / "siwah-tui"
    monkeypatch.setattr(cli_main, "ensure_tui_binary", lambda **_kwargs: target)
    runner = CliRunner()
    result = runner.invoke(app, ["install-tui"])
    assert result.exit_code == 0
    assert str(target) in result.stdout


def test_doctor_with_empty_home_creates_default_catalog(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SIWAH_CONFIG", raising=False)
    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "opencode-go" in result.stdout
    assert "openai" in result.stdout
    assert "credential=missing" in result.stdout
    assert "state db: ok" in result.stdout


def test_auth_login_and_status(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    config = tmp_path / "config.toml"
    config.write_text(
        """
[providers.mock]
api = "openai_chat"
base_url = "https://mock.invalid/v1"
auth = { scheme = "bearer", env = "MOCK_API_KEY" }
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("SIWAH_CONFIG", str(config))
    runner = CliRunner()
    result = runner.invoke(app, ["auth", "login", "mock"], input="secret-key\n")
    assert result.exit_code == 0, result.output
    assert "Saved credential" in result.stdout

    status = runner.invoke(app, ["auth", "status"])
    assert status.exit_code == 0
    assert "mock:" in status.stdout
    assert "secret-key" not in status.stdout


def test_auth_login_no_argument_adds_missing_presets(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    config = tmp_path / "config.toml"
    config.write_text(
        """
[providers.custom]
api = "openai_chat"
base_url = "https://custom.invalid/v1"
auth = { scheme = "none" }
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("SIWAH_CONFIG", str(config))
    result = CliRunner().invoke(app, ["auth", "login"], input="custom\n")
    assert result.exit_code == 0, result.output
    text = config.read_text(encoding="utf-8")
    assert "[providers.custom]" in text
    assert "[providers.chatgpt]" in text


def test_auth_login_unknown_preset_does_not_write_config(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    config = tmp_path / "config.toml"
    config.write_text(
        """
[providers.custom]
api = "openai_chat"
base_url = "https://custom.invalid/v1"
auth = { scheme = "none" }
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("SIWAH_CONFIG", str(config))
    before = config.read_text(encoding="utf-8")
    result = CliRunner().invoke(app, ["auth", "login", "chatgp"])
    assert result.exit_code == 2
    assert "Unknown provider" in result.output
    assert config.read_text(encoding="utf-8") == before


def test_auth_logout_survives_a_broken_config(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    config = tmp_path / "config.toml"
    config.write_text("not valid toml [[[", encoding="utf-8")
    monkeypatch.setenv("SIWAH_CONFIG", str(config))
    store = cli_main.AuthStore()
    store.save_key("mock", "secret-key")
    result = CliRunner().invoke(app, ["auth", "logout", "mock"])
    assert result.exit_code == 0, result.output
    assert store.get_key("mock", AuthConfig()) is None


def test_models_list_accepts_a_chatgpt_account(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    config = tmp_path / "config.toml"
    config.write_text(
        """
[providers.chatgpt]
api = "openai_responses"
base_url = "https://api.openai.com/v1"
models_url = "https://api.openai.com/v1/models"
auth = { scheme = "bearer", browser = "chatgpt" }
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("SIWAH_CONFIG", str(config))
    monkeypatch.setattr(cli_main, "_chatgpt_connected", lambda: True)

    async def list_models(_self, _provider):
        return []

    monkeypatch.setattr(cli_main.ProviderRegistry, "list_models", list_models)
    result = CliRunner().invoke(app, ["models", "list"])
    assert result.exit_code == 0, result.output
    assert "credential missing" not in result.output


def test_auth_status_reports_a_corrupt_chatgpt_store(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    config = tmp_path / "config.toml"
    config.write_text(
        """
[providers.chatgpt]
api = "openai_responses"
base_url = "https://api.openai.com/v1"
models_url = "https://api.openai.com/v1/models"
auth = { scheme = "bearer", browser = "chatgpt" }
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("SIWAH_CONFIG", str(config))
    path = tmp_path / "home" / "chatgpt" / "auth.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    result = CliRunner().invoke(app, ["auth", "status"])
    assert result.exit_code == 0
    assert "Cannot read the local ChatGPT credential store" in result.output


def test_run_requires_model(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SIWAH_CONFIG", raising=False)
    runner = CliRunner()
    result = runner.invoke(app, ["run", "hello"])
    assert result.exit_code == 2
    assert "No model configured" in result.stdout or "No model configured" in result.output


def test_plain_renderer_survives_ascii_only_stream():
    class AsciiStream:
        encoding = "ascii"

        def __init__(self) -> None:
            self.value = ""

        def write(self, value: str) -> None:
            value.encode(self.encoding)  # raises for non-ascii, like a cp1252 console
            self.value += value

        def flush(self) -> None:
            return None

    stream = AsciiStream()
    PlainRenderer()._write("fixed → done", stream=stream)  # type: ignore[arg-type]
    assert "fixed" in stream.value
    assert "done" in stream.value


def test_run_keyboard_interrupt_exits_130(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    config = Config(
        providers={
            "mock": ProviderConfig(
                api="openai_chat",
                base_url="https://mock.invalid/v1",
                auth=AuthConfig(scheme="none"),
            )
        },
        roles=RolesConfig(main="mock/model-a"),
    )
    monkeypatch.setattr(cli_main, "load_config", lambda: config)

    class InterruptingLoop:
        def __init__(self, **_kwargs) -> None:
            pass

        async def run(self, *_args, **_kwargs):
            raise KeyboardInterrupt

    monkeypatch.setattr(agent_loop, "AgentLoop", InterruptingLoop)
    result = CliRunner().invoke(
        app,
        ["run", "hello", "--cwd", str(tmp_path), "--model", "mock/model-a"],
    )
    assert result.exit_code == 130
    assert "Aborted" in result.output


def test_models_list_explains_missing_credentials(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SIWAH_CONFIG", raising=False)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    runner = CliRunner()
    result = runner.invoke(app, ["models", "list"])
    assert result.exit_code == 2
    assert "credential missing" in result.stdout
    assert "opencode-go" in result.stdout
    assert "openai" in result.stdout


def test_skills_and_memory_commands(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SIWAH_CONFIG", raising=False)
    from siwah.harness.memory import write_memory
    from siwah.harness.skills import create_skill

    create_skill("cli-skill", "A CLI skill", "Use this skill from the CLI.")
    write_memory(tmp_path, target="user", content="CLI memory content.")
    runner = CliRunner()
    listed = runner.invoke(app, ["skills", "list"])
    assert listed.exit_code == 0
    assert "cli-skill" in listed.stdout
    viewed = runner.invoke(app, ["skills", "view", "cli-skill"])
    assert viewed.exit_code == 0
    assert "Use this skill from the CLI." in viewed.stdout
    memory = runner.invoke(app, ["memory", "show", "--cwd", str(tmp_path)])
    assert memory.exit_code == 0
    assert "CLI memory content." in memory.stdout


def test_run_ultra_requires_a_clean_git_workspace(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    config = Config(
        providers={
            "mock": ProviderConfig(
                api="openai_chat",
                base_url="https://mock.invalid/v1",
                auth=AuthConfig(scheme="none"),
            )
        },
        roles=RolesConfig(main="mock/model-a"),
    )
    monkeypatch.setattr(cli_main, "load_config", lambda: config)
    result = CliRunner().invoke(
        app,
        [
            "run",
            "hello",
            "--cwd",
            str(tmp_path),
            "--model",
            "mock/model-a",
            "--ultra",
            "2",
        ],
    )
    assert result.exit_code == 1
    assert "best-of-n: rejected" in result.output
