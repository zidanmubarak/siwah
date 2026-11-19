from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from siwah.config import AgentConfig, PermissionsConfig
from siwah.permissions.policy import Approval, PermissionPolicy, PermissionRequest
from siwah.repo.lsp import find_references_lsp, server_for
from siwah.tools.base import ToolContext
from siwah.tools.repo import RefsTool


def _context(workspace: Path) -> ToolContext:
    async def approver(_request: PermissionRequest) -> Approval:
        return Approval(decision="once")

    return ToolContext(
        workspace=workspace,
        policy=PermissionPolicy(workspace, PermissionsConfig(mode="auto-edit")),
        session_id="ses_lsp",
        agent_config=AgentConfig(),
        approver=approver,
    )


def test_refs_tool_uses_lsp_for_python(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "mod.py").write_text(
        "def target(x):\n    return x\n\ndef caller():\n    return target(1)\n",
        encoding="utf-8",
    )
    if server_for(workspace / "mod.py") is None:
        pytest.skip("pyright-langserver is not available")
    if asyncio.run(find_references_lsp(workspace, "target", limit=5)) is None:
        pytest.skip("language server could not initialize in this environment")
    result = asyncio.run(RefsTool().run(_context(workspace), query="target", limit=20))
    assert result.ok, result.error
    assert result.data and result.data.get("engine") == "lsp"
    assert "mod.py:5" in result.output


def test_refs_tool_falls_back_when_lsp_is_disabled(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "mod.py").write_text(
        "def target(x):\n    return x\n\ndef caller():\n    return target(1)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SIWAH_LSP", "off")
    result = asyncio.run(RefsTool().run(_context(workspace), query="target", limit=20))
    assert result.ok, result.error
    assert result.data and result.data.get("engine") == "fallback"
    assert "mod.py:5" in result.output
