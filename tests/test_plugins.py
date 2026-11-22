from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from siwah.commands import list_commands_for
from siwah.core.subagents import load_subagents
from siwah.harness.skills import list_skills
from siwah.hooks import load_hooks
from siwah.mcp.client import McpManager, load_mcp_config
from siwah.paths import harness_dir
from siwah.plugins import (
    PluginError,
    install_plugin,
    list_plugins,
    remove_plugin,
    set_enabled,
)


def _make_plugin(root: Path, name: str = "demo") -> Path:
    root.mkdir(parents=True)
    (root / "plugin.toml").write_text(
        f'name = "{name}"\nversion = "1.2.3"\ndescription = "Demo bundle"\n',
        encoding="utf-8",
    )
    skill = root / "skills" / "demo-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demo skill\n---\n\nUse the demo workflow.\n",
        encoding="utf-8",
    )
    commands = root / "commands"
    commands.mkdir()
    (commands / "hello.md").write_text(
        "---\nname: hello\ndescription: Say hello\n---\n\nSay hello.\n",
        encoding="utf-8",
    )
    subagents = root / "subagents"
    subagents.mkdir()
    (subagents / "helper.md").write_text(
        "---\nname: helper\ndescription: Help with the demo\nread_only: true\n---\n\nHelp.\n",
        encoding="utf-8",
    )
    hooks = root / "hooks"
    hooks.mkdir()
    (hooks / "noop.json").write_text(
        json.dumps(
            {
                "name": "demo-noop",
                "event": "SessionStart",
                "command": ["python", "-c", "pass"],
            }
        ),
        encoding="utf-8",
    )
    (root / "mcp.json").write_text(
        json.dumps(
            {
                "servers": {
                    "demo-http": {
                        "transport": "http",
                        "url": "https://mcp.example.com/mcp",
                        "auth": "oauth",
                        "scopes": ["profile"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return root


def test_install_and_load_plugin_content(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    source = _make_plugin(tmp_path / "source")
    info = install_plugin(source)

    assert info.name == "demo"
    assert [item.name for item in list_plugins()] == ["demo"]
    assert [item.name for item in list_skills()] == ["demo-skill"]
    assert [item.name for item in list_commands_for()] == ["hello"]
    assert "helper" in load_subagents()
    assert [hook.name for hook in load_hooks()] == ["demo-noop"]
    assert load_mcp_config()["servers"]["demo-http"]["auth"] == "oauth"


def test_user_mcp_config_wins_over_a_plugin(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    install_plugin(_make_plugin(tmp_path / "source"))
    harness_dir().mkdir(parents=True, exist_ok=True)
    (harness_dir() / "mcp.json").write_text(
        json.dumps(
            {
                "servers": {
                    "demo-http": {
                        "transport": "http",
                        "url": "https://user.example.com/mcp",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    servers = load_mcp_config()["servers"]

    assert servers["demo-http"]["url"] == "https://user.example.com/mcp"


async def test_mcp_manager_reload_keeps_plugin_servers(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    install_plugin(_make_plugin(tmp_path / "source"))
    manager = McpManager.load()
    try:
        await manager.reload()
        assert "demo-http" in manager.servers
    finally:
        await manager.close()


def test_disable_and_remove_plugin(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    install_plugin(_make_plugin(tmp_path / "source"))

    set_enabled("demo", False)
    assert list_skills() == []
    assert "demo-http" not in load_mcp_config()["servers"]
    assert load_hooks() == []

    set_enabled("demo", True)
    assert [item.name for item in list_skills()] == ["demo-skill"]

    remove_plugin("demo")
    assert list_plugins() == []
    assert not (tmp_path / "home" / "plugins" / "demo").exists()


def test_install_plugin_zip(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    source = _make_plugin(tmp_path / "source")
    archive = tmp_path / "demo.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for path in source.rglob("*"):
            if path.is_file():
                bundle.write(path, arcname=str(path.relative_to(source)))

    info = install_plugin(archive)

    assert info.name == "demo"
    assert (tmp_path / "home" / "plugins" / "demo" / "plugin.toml").is_file()


def test_install_plugin_rejects_zip_traversal(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("plugin.toml", 'name = "bad"\n')
        bundle.writestr("../escape.txt", "nope")

    with pytest.raises(PluginError, match="escapes"):
        install_plugin(archive)
