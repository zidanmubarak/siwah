from __future__ import annotations

import asyncio
from pathlib import Path

from siwah.config import AgentConfig, PermissionsConfig
from siwah.permissions.policy import Approval, PermissionRequest
from siwah.tools.base import ToolContext
from siwah.tools.fs import EditFileTool, GlobTool, GrepTool, ReadFileTool, WriteFileTool
from siwah.tools.repo import RepoMapTool
from siwah.tools.runtime import BashTool, TodoTool, _looks_long_running


def make_context(workspace: Path, **kwargs) -> ToolContext:
    from siwah.permissions.policy import PermissionPolicy

    async def approver(request: PermissionRequest) -> Approval:
        return Approval(decision="once")

    return ToolContext(
        workspace=workspace,
        policy=PermissionPolicy(workspace, PermissionsConfig(mode="auto-edit")),
        session_id="ses_test",
        agent_config=AgentConfig(),
        approver=approver,
        **kwargs,
    )


def test_read_write_edit_and_stale(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    context = make_context(workspace)

    written = asyncio.run(WriteFileTool().run(context, path="note.txt", content="hello\nworld\n"))
    assert written.ok
    edited = asyncio.run(
        EditFileTool().run(context, path="note.txt", old_string="world", new_string="siwah")
    )
    assert edited.ok
    assert (workspace / "note.txt").read_text() == "hello\nsiwah\n"

    (workspace / "note.txt").write_text("external change\n")
    stale = asyncio.run(
        EditFileTool().run(context, path="note.txt", old_string="external", new_string="x")
    )
    assert not stale.ok
    assert "stale" in (stale.error or "").lower()


def test_write_file_marks_replaced_files_as_edits(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    context = make_context(workspace)
    (workspace / "note.txt").write_text("old line\n", encoding="utf-8")

    result = asyncio.run(WriteFileTool().run(context, path="note.txt", content="new line\n"))
    assert result.ok
    assert result.data
    assert result.data["edited"] is True
    assert result.data["full_output"] == "new line\n"
    assert "--- a/note.txt" in result.data["diff"]

    created = asyncio.run(WriteFileTool().run(context, path="fresh.txt", content="fresh\n"))
    assert created.ok
    assert created.data
    assert created.data["edited"] is False


def test_glob_and_grep(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.py").write_text("VALUE = 1\n")
    (workspace / "b.txt").write_text("VALUE = 2\n")
    context = make_context(workspace)

    glob = asyncio.run(GlobTool().run(context, pattern="*.py"))
    assert "a.py" in glob.output
    assert "b.txt" not in glob.output

    grep = asyncio.run(GrepTool().run(context, pattern="VALUE", glob="*.py"))
    assert "a.py:1" in grep.output
    assert "b.txt" not in grep.output


def test_repo_map_has_no_budget_knob():
    parameters = RepoMapTool().parameters
    assert "budget_tokens" not in parameters.get("properties", {})


def test_long_running_detection():
    assert _looks_long_running("python -m http.server 8123")
    assert _looks_long_running("npm run dev")
    assert _looks_long_running("vite")
    assert not _looks_long_running("npx vitest run")
    assert not _looks_long_running("pytest -q")


def test_bash_and_todo(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    context = make_context(workspace)
    result = asyncio.run(BashTool().run(context, command="echo siwah-ok"))
    assert result.ok
    assert "siwah-ok" in result.output

    server = asyncio.run(BashTool().run(context, command="python -m http.server 8123"))
    assert not server.ok
    assert "background=true" in (server.error or "")

    todo = asyncio.run(
        TodoTool().run(
            context,
            items=[{"content": "write tests"}, {"content": "run lint"}],
        )
    )
    assert "write tests" in todo.output
    listed = asyncio.run(TodoTool().run(context))
    assert "run lint" in listed.output


def test_fuzzy_whitespace_and_unified_patch_edits(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    context = make_context(workspace)
    (workspace / "math_utils.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")

    read = asyncio.run(ReadFileTool().run(context, path="math_utils.py"))
    assert read.ok
    fuzzy = asyncio.run(
        EditFileTool().run(
            context,
            path="math_utils.py",
            old_string="	return a - b",
            new_string="    return a + b",
        )
    )
    assert fuzzy.ok
    assert "return a + b" in (workspace / "math_utils.py").read_text()

    read = asyncio.run(ReadFileTool().run(context, path="math_utils.py"))
    assert read.ok
    patch = (
        "--- a/math_utils.py\n"
        "+++ b/math_utils.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        "-    return a + b\n"
        "+    return a * b\n"
    )
    applied = asyncio.run(EditFileTool().run(context, path="math_utils.py", patch=patch))
    assert applied.ok, applied.error
    assert "return a * b" in (workspace / "math_utils.py").read_text()


def test_invalid_syntax_is_rejected_before_write(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    context = make_context(workspace)
    rejected = asyncio.run(
        WriteFileTool().run(context, path="broken.py", content="def f(:\n    pass\n")
    )
    assert not rejected.ok
    assert "syntax" in (rejected.error or "").lower()
    assert not (workspace / "broken.py").exists()

    (workspace / "ok.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    read = asyncio.run(ReadFileTool().run(context, path="ok.py"))
    assert read.ok
    invalid_edit = asyncio.run(
        EditFileTool().run(
            context,
            path="ok.py",
            old_string="return 1",
            new_string="return (",
        )
    )
    assert not invalid_edit.ok
    assert "syntax" in (invalid_edit.error or "").lower()
    assert "return 1" in (workspace / "ok.py").read_text()
