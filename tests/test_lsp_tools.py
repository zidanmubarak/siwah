from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from siwah.config import AgentConfig, PermissionsConfig
from siwah.permissions.policy import Approval, PermissionPolicy, PermissionRequest
from siwah.repo.lsp import ServerSpec
from siwah.tools import lsp as lsp_tools
from siwah.tools.base import ToolContext
from siwah.tools.lsp import LspDiagnosticsTool, LspRenameTool


def _context(workspace: Path) -> ToolContext:
    async def approver(_request: PermissionRequest) -> Approval:
        return Approval(decision="once")

    return ToolContext(
        workspace=workspace,
        policy=PermissionPolicy(workspace, PermissionsConfig(mode="auto-edit")),
        session_id="ses_lsp_tools",
        agent_config=AgentConfig(),
        approver=approver,
    )


class _DiagnosticsSession:
    def __init__(self, *_args, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def diagnostics(self, _path, *, timeout=10.0):
        return [
            {
                "message": "Name is not defined",
                "severity": 1,
                "range": {"start": {"line": 0, "character": 0}},
            }
        ]


def test_lsp_diagnostics_formats_errors(tmp_path: Path, monkeypatch):
    path = tmp_path / "mod.py"
    path.write_text("print(missing)\n", encoding="utf-8")
    monkeypatch.setattr(lsp_tools, "LspSession", _DiagnosticsSession)
    monkeypatch.setattr(lsp_tools, "server_for", lambda _path: ServerSpec(("fixture",), "python"))
    result = asyncio.run(LspDiagnosticsTool().run(_context(tmp_path), path="mod.py"))
    assert result.ok
    assert "mod.py:1:1: error" in result.output
    assert "Name is not defined" in result.output


class _RenameSession:
    def __init__(self, *_args, **_kwargs):
        self.path = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def rename(self, path, *, line, character, new_name):
        self.path = path
        return {
            "changes": {
                path.as_uri(): [
                    {
                        "range": {
                            "start": {"line": 0, "character": 4},
                            "end": {"line": 0, "character": 7},
                        },
                        "newText": new_name,
                    }
                ]
            }
        }


def test_lsp_rename_previews_then_applies(tmp_path: Path, monkeypatch):
    path = tmp_path / "mod.py"
    path.write_text("def old():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(lsp_tools, "LspSession", _RenameSession)
    monkeypatch.setattr(lsp_tools, "server_for", lambda _path: ServerSpec(("fixture",), "python"))
    context = _context(tmp_path)

    preview = asyncio.run(
        LspRenameTool().run(context, path="mod.py", line=1, column=5, new_name="new", apply=False)
    )
    assert preview.ok
    assert "Rename preview" in preview.output
    assert path.read_text(encoding="utf-8").startswith("def old")

    applied = asyncio.run(
        LspRenameTool().run(context, path="mod.py", line=1, column=5, new_name="new", apply=True)
    )
    assert applied.ok
    assert path.read_text(encoding="utf-8").startswith("def new")


def test_apply_edits_rejects_overlaps():
    with pytest.raises(ValueError, match="overlapping"):
        lsp_tools._apply_edits(
            "abc",
            [
                {
                    "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 0, "character": 2},
                    },
                    "newText": "x",
                },
                {
                    "range": {
                        "start": {"line": 0, "character": 1},
                        "end": {"line": 0, "character": 3},
                    },
                    "newText": "y",
                },
            ],
        )
