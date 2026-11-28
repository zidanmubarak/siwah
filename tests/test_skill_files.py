from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from siwah.config import AgentConfig, PermissionsConfig
from siwah.harness.bootstrap import ensure_harness
from siwah.harness.skills import (
    create_skill,
    list_skill_files,
    skill_file_path,
    write_skill_file,
)
from siwah.permissions.policy import Approval, PermissionPolicy, PermissionRequest
from siwah.tools.base import ToolContext
from siwah.tools.harness import SkillManageTool, SkillViewTool


def _context(workspace: Path) -> ToolContext:
    async def approver(_request: PermissionRequest) -> Approval:
        return Approval(decision="once")

    return ToolContext(
        workspace=workspace,
        policy=PermissionPolicy(workspace, PermissionsConfig(mode="auto-edit")),
        session_id="ses_skill_files",
        agent_config=AgentConfig(),
        approver=approver,
    )


def test_skill_support_files_are_scoped_and_viewable(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    ensure_harness()
    workspace = tmp_path / "repo"
    workspace.mkdir()
    create_skill(
        "deploy",
        "Deploy the service.",
        "## When to Use\nUse when deploying.\n\n## Procedure\nRun the script.",
    )
    path = write_skill_file("deploy", "references/guide.md", "Details here.", workspace)
    assert path.read_text(encoding="utf-8") == "Details here."
    assert list_skill_files("deploy", workspace) == ["references/guide.md"]
    with pytest.raises(ValueError):
        skill_file_path("deploy", "../escape.md", workspace)

    context = _context(workspace)
    listed = asyncio.run(SkillManageTool().run(context, action="list_files", name="deploy"))
    assert "references/guide.md" in listed.output
    viewed = asyncio.run(
        SkillViewTool().run(context, name="deploy", file_path="references/guide.md")
    )
    assert viewed.ok and viewed.output == "Details here."
    lint = asyncio.run(SkillManageTool().run(context, action="lint", name="deploy"))
    assert lint.ok
