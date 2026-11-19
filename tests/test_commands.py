from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from siwah.commands import find_command, list_commands, list_commands_for
from siwah.server.app import create_app

from .test_server import make_runtime


def test_command_loader_and_arguments(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    directory = tmp_path / "home" / "harness" / "commands"
    directory.mkdir(parents=True)
    (directory / "review.md").write_text(
        "---\nname: review\ndescription: Review a file\n---\nReview $ARGUMENTS carefully.\n",
        encoding="utf-8",
    )
    commands = list_commands()
    assert [command.name for command in commands] == ["review"]
    command = find_command("/review")
    assert command is not None
    assert command.render("app.py") == "Review app.py carefully."


def test_project_commands_override_global_commands(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    global_dir = tmp_path / "home" / "harness" / "commands"
    global_dir.mkdir(parents=True)
    (global_dir / "ship.md").write_text(
        "---\nname: ship\ndescription: Global ship\n---\nGlobal ship.\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    project_dir = workspace / ".siwah" / "commands"
    project_dir.mkdir(parents=True)
    (project_dir / "ship.md").write_text(
        "---\nname: ship\ndescription: Project ship\n---\nProject ship.\n",
        encoding="utf-8",
    )
    (project_dir / "focus.md").write_text(
        "---\nname: focus\ndescription: Focus the work\n---\nFocus.\n",
        encoding="utf-8",
    )

    commands = list_commands_for(workspace)
    assert {command.name: command.description for command in commands}["ship"] == "Project ship"
    assert [command.name for command in commands].count("ship") == 1
    assert "focus" in [command.name for command in commands]


@pytest.mark.asyncio
async def test_commands_endpoint(config, store, sample_repo: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    directory = tmp_path / "home" / "harness" / "commands"
    directory.mkdir(parents=True)
    (directory / "ship.md").write_text(
        "---\nname: ship\ndescription: Ship it\n---\nShip $ARGUMENTS.\n",
        encoding="utf-8",
    )
    runtime = await make_runtime(config, store, sample_repo)
    app = create_app(config, runtime=runtime, token="test-token")
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/v1/commands", headers={"Authorization": "Bearer test-token"}
            )
        assert response.status_code == 200
        payload = response.json()
        assert payload["commands"][0]["name"] == "ship"
        assert payload["commands"][0]["body"] == "Ship $ARGUMENTS."
    finally:
        await runtime.close()
