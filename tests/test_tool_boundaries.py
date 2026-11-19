"""Regression tests for tool authorization, mutation, and resource boundaries."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from siwah.config import AgentConfig, PermissionRule, PermissionsConfig, SandboxConfig
from siwah.permissions.policy import Approval, PermissionPolicy, PermissionRequest
from siwah.repo import lsp
from siwah.repo.index import WorkspaceIndex
from siwah.sandbox import Sandbox
from siwah.tools import fs, runtime
from siwah.tools.base import ToolContext
from siwah.tools.fs import EditFileTool, GlobTool, GrepTool, ReadFileTool, WriteFileTool
from siwah.tools.repo import RefsTool, RepoMapTool, SymbolsTool


def context_for(workspace: Path, rules: list[PermissionRule] | None = None) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        policy=PermissionPolicy(workspace, PermissionsConfig(mode="yolo", rules=rules or [])),
        session_id="ses_boundary",
        agent_config=AgentConfig(),
    )


@pytest.mark.parametrize("operation", ["edit", "whole_edit", "write", "create"])
async def test_approval_race_preserves_human_bytes(tmp_path: Path, operation: str):
    context = context_for(tmp_path)
    context.policy.config.mode = "ask"
    path = tmp_path / "note.txt"
    original = b"before\r\nsecond\r\n"
    human = b"human update\r\nsecond\r\n"
    if operation != "create":
        path.write_bytes(original)
        assert (await ReadFileTool().run(context, path="note.txt")).ok

    async def approve(_request: PermissionRequest) -> Approval:
        # A real file changes while the tool is awaiting a UI approval.
        await asyncio.sleep(0)
        path.write_bytes(human)
        return Approval(decision="once")

    context.approver = approve
    if operation == "edit":
        result = await EditFileTool().run(
            context, path="note.txt", old_string="before", new_string="agent"
        )
    elif operation == "whole_edit":
        result = await EditFileTool().run(context, path="note.txt", content="agent\n")
    else:
        result = await WriteFileTool().run(context, path="note.txt", content="agent\n")
    assert not result.ok
    assert "stale" in (result.error or "").lower()
    assert path.read_bytes() == human
    assert not list(tmp_path.glob(".siwah-*"))


async def test_write_replacement_rejects_stored_stale_hash(tmp_path: Path):
    context = context_for(tmp_path)
    path = tmp_path / "note.txt"
    path.write_bytes(b"before\r\n")
    await ReadFileTool().run(context, path="note.txt")
    path.write_bytes(b"human\r\n")
    result = await WriteFileTool().run(context, path="note.txt", content="agent\n")
    assert not result.ok
    assert path.read_bytes() == b"human\r\n"


async def test_successful_edit_preserves_crlf(tmp_path: Path):
    (tmp_path / "note.txt").write_bytes(b"before\r\nsecond\r\n")
    result = await EditFileTool().run(
        context_for(tmp_path), path="note.txt", old_string="before", new_string="after"
    )
    assert result.ok
    assert (tmp_path / "note.txt").read_bytes() == b"after\r\nsecond\r\n"


@pytest.mark.parametrize("tool", [GlobTool(), GrepTool(), RepoMapTool(), SymbolsTool(), RefsTool()])
async def test_search_explicit_tool_denial(tmp_path: Path, tool):
    (tmp_path / "public.py").write_text("def sentinel(): pass\n", encoding="utf-8")
    context = context_for(tmp_path, [PermissionRule(tool=tool.name, decision="deny")])
    result = await tool.run(context, pattern="sentinel", query="sentinel")
    assert not result.ok
    assert "sentinel" not in result.output


@pytest.mark.parametrize("tool", [GlobTool(), GrepTool(), RepoMapTool(), SymbolsTool(), RefsTool()])
@pytest.mark.parametrize("denied_tool", ["read_file", "search_tool"])
async def test_search_filters_denied_descendants(tmp_path: Path, monkeypatch, tool, denied_tool):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SIWAH_LSP", "off")
    (tmp_path / "public.py").write_text("def visible(): pass\n", encoding="utf-8")
    (tmp_path / "private.py").write_text("def hidden_sentinel(): pass\n", encoding="utf-8")
    context = context_for(
        tmp_path,
        [
            PermissionRule(
                tool=tool.name if denied_tool == "search_tool" else denied_tool,
                path="private.py",
                decision="deny",
            )
        ],
    )
    result = await tool.run(
        context, pattern="sentinel" if tool.name == "grep" else "*", query="sentinel"
    )
    assert result.ok, result.error
    assert "private.py" not in result.output
    assert "hidden_sentinel" not in result.output


@pytest.mark.parametrize("tool", [GlobTool(), GrepTool(), RepoMapTool(), SymbolsTool(), RefsTool()])
async def test_search_asks_once_for_root_and_skips_denied_leaf(tmp_path: Path, monkeypatch, tool):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SIWAH_LSP", "off")
    (tmp_path / "ok.py").write_text("def sentinel(): pass\n", encoding="utf-8")
    (tmp_path / "private.py").write_text("def hidden_sentinel(): pass\n", encoding="utf-8")
    context = context_for(
        tmp_path,
        [
            PermissionRule(tool=tool.name, decision="ask"),
            PermissionRule(tool="read_file", path="private.py", decision="deny"),
        ],
    )
    context.approver = AsyncMock(return_value=Approval(decision="once"))
    result = await tool.run(
        context, pattern="sentinel" if tool.name == "grep" else "*.py", query="sentinel"
    )
    assert result.ok
    assert "ok.py" in result.output
    assert "private.py" not in result.output
    context.approver.assert_awaited_once()


@pytest.mark.parametrize("backend", ["local", "missing-docker", "docker", "bwrap"])
async def test_sandbox_background_fails_closed_before_host_job(tmp_path: Path, backend: str):
    context = context_for(tmp_path)
    context.jobs = AsyncMock()
    context.sandbox = AsyncMock()
    context.sandbox.backend = lambda: backend
    result = await runtime.BashTool().run(context, command="echo unsafe", background=True)
    assert not result.ok
    assert "sandbox" in result.output.lower()
    context.jobs.start.assert_not_called()


class PendingProcess:
    """No OS processes are created; EOF is controlled by the termination spy."""

    pid = 123456
    returncode: int | None = None

    def __init__(self):
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.drained = asyncio.Event()
        self.reader_cancelled = False

    async def communicate(self):
        self.started.set()
        try:
            await self.stopped.wait()
            return b"partial output", None
        except asyncio.CancelledError:
            self.reader_cancelled = True
            raise
        finally:
            self.drained.set()


@pytest.mark.parametrize("sandboxed", [False, True])
async def test_cancellation_kills_process_and_drains_reader(tmp_path: Path, monkeypatch, sandboxed):
    process = PendingProcess()
    spawn = AsyncMock(return_value=process)
    terminate = AsyncMock(side_effect=lambda _process: process.stopped.set())
    # On POSIX, cleanup also escalates an unresponsive process group.
    monkeypatch.setattr("siwah.jobs.os.killpg", Mock(), raising=False)
    module = "siwah.sandbox" if sandboxed else "siwah.tools.runtime"
    monkeypatch.setattr(f"{module}.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr(f"{module}.terminate_process_tree", terminate)
    if sandboxed:
        sandbox = Sandbox(SandboxConfig(backend="docker", image="fixture"), cwd=tmp_path)
        monkeypatch.setattr(sandbox, "backend", lambda: "docker")
        remove = AsyncMock()
        monkeypatch.setattr(sandbox, "_remove_container", remove)
        task = asyncio.create_task(sandbox.run(["sleep", "30"]))
    else:
        task = asyncio.create_task(
            runtime.BashTool().run(context_for(tmp_path), command="sleep 30")
        )
    await process.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    try:
        terminate.assert_awaited_once_with(process)
        assert process.drained.is_set()
        assert not process.reader_cancelled
        if sandboxed:
            remove.assert_awaited_once()
            assert remove.call_args.args[0].startswith("siwah-sandbox-")
    finally:
        # Keep the red test from leaving the intentionally orphaned reader pending.
        process.stopped.set()
        await process.drained.wait()


async def test_second_cancel_waits_for_cleanup(tmp_path: Path, monkeypatch):
    process = PendingProcess()
    terminating = asyncio.Event()
    release = asyncio.Event()

    async def terminate(_process):
        terminating.set()
        await release.wait()
        process.stopped.set()

    monkeypatch.setattr("siwah.jobs.os.killpg", Mock(), raising=False)
    monkeypatch.setattr(runtime.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    monkeypatch.setattr(runtime, "terminate_process_tree", terminate)
    task = asyncio.create_task(runtime.BashTool().run(context_for(tmp_path), command="sleep 30"))
    await process.started.wait()
    task.cancel()
    await terminating.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.drained.is_set()
    assert not process.reader_cancelled


@pytest.mark.parametrize("sandboxed", [False, True])
async def test_timeout_still_terminates_and_drains(tmp_path: Path, monkeypatch, sandboxed: bool):
    process = PendingProcess()
    terminate = AsyncMock(side_effect=lambda _process: process.stopped.set())
    monkeypatch.setattr("siwah.jobs.os.killpg", Mock(), raising=False)
    module = "siwah.sandbox" if sandboxed else "siwah.tools.runtime"
    monkeypatch.setattr(f"{module}.asyncio.create_subprocess_exec", AsyncMock(return_value=process))
    monkeypatch.setattr(f"{module}.terminate_process_tree", terminate)
    if sandboxed:
        sandbox = Sandbox(SandboxConfig(backend="local"), cwd=tmp_path)
        result = await sandbox.run(["sleep", "30"], timeout_s=0.01)
    else:
        result = await runtime.BashTool().run(
            context_for(tmp_path), command="sleep 30", timeout_s=0.01
        )
        assert "partial output" in result.output
    assert not result.ok
    assert "timed out" in result.output
    terminate.assert_awaited_once_with(process)
    assert process.drained.is_set()
    assert not process.reader_cancelled


def mock_web(monkeypatch, handler):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        runtime.httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs)
    )


@pytest.mark.parametrize(
    "destination",
    [
        "http://127.0.0.1/private",
        "http://10.2.3.4/private",
        "http://[::1]/private",
        "http://[fc00::1]/private",
        "http://[::ffff:127.0.0.1]/private",
        "file:///private",
    ],
)
async def test_redirect_revalidates_destination(tmp_path: Path, monkeypatch, destination: str):
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        if len(seen) == 1:
            return httpx.Response(302, headers={"location": destination})
        return httpx.Response(200, text="INTERNAL_SENTINEL")

    mock_web(monkeypatch, handler)
    result = await runtime.WebFetchTool().run(context_for(tmp_path), url="https://public.invalid/")
    assert not result.ok
    assert seen == ["https://public.invalid/"]
    assert "INTERNAL_SENTINEL" not in result.output


async def test_redirect_rechecks_url_permission(tmp_path: Path, monkeypatch):
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://blocked.invalid/private"})

    mock_web(monkeypatch, handler)
    context = context_for(
        tmp_path,
        [PermissionRule(tool="web_fetch", command="https://blocked.invalid/*", decision="deny")],
    )
    result = await runtime.WebFetchTool().run(context, url="https://public.invalid/")
    assert not result.ok
    assert seen == ["https://public.invalid/"]


async def test_public_relative_redirect_is_authorized(tmp_path: Path, monkeypatch):
    def handler(request):
        if request.url.path == "/":
            return httpx.Response(302, headers={"location": "/next"})
        return httpx.Response(200, text="public text")

    mock_web(monkeypatch, handler)
    context = context_for(tmp_path, [PermissionRule(tool="web_fetch", decision="ask")])
    context.approver = AsyncMock(return_value=Approval(decision="once"))
    result = await runtime.WebFetchTool().run(context, url="https://public.invalid/")
    assert result.ok
    assert result.output == "public text"
    assert context.approver.await_count == 2


async def test_redirects_are_bounded(tmp_path: Path, monkeypatch):
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": f"/hop{len(seen)}"})

    mock_web(monkeypatch, handler)
    result = await runtime.WebFetchTool().run(context_for(tmp_path), url="https://public.invalid/")
    assert not result.ok
    assert "redirect" in result.output.lower()
    assert len(seen) <= 11


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.1",
        "2130706433",
        "0x7f000001",
        "127。0。0。1",
        "localhost.",
        "[::1]",
        "[fe80::1]",
        "[fc00::1]",
    ],
)
async def test_initial_private_address_is_never_requested(tmp_path: Path, monkeypatch, host: str):
    handler = Mock(return_value=httpx.Response(200, text="INTERNAL_SENTINEL"))
    mock_web(monkeypatch, handler)
    result = await runtime.WebFetchTool().run(context_for(tmp_path), url=f"http://{host}/")
    assert not result.ok
    handler.assert_not_called()


@pytest.mark.parametrize("query", ["MISSING_IDENTIFIER", "imported_target"])
async def test_missing_identifier_does_not_launch_unrelated_lsp(tmp_path: Path, monkeypatch, query):
    monkeypatch.delenv("SIWAH_LSP", raising=False)
    (tmp_path / "mod.py").write_text(
        "from other import imported_target\n\ndef unrelated(): pass\n", encoding="utf-8"
    )
    index = WorkspaceIndex(tmp_path, cache_root=tmp_path / "cache")
    session = Mock()
    monkeypatch.setattr(lsp, "LspSession", session)
    monkeypatch.setattr(lsp, "server_for", lambda _: lsp.ServerSpec(("fixture",), "python"))
    result = await lsp.find_references_lsp(tmp_path, query, index=index)
    assert result is None
    session.assert_not_called()


async def test_lsp_filters_reference_paths_before_reading(tmp_path: Path, monkeypatch):
    public = tmp_path / "public.py"
    private = tmp_path / "private.py"
    public.write_text("target()\n", encoding="utf-8")
    private.write_text("HIDDEN_SENTINEL = target()\n", encoding="utf-8")
    context = context_for(
        tmp_path, [PermissionRule(tool="read_file", path="private.py", decision="deny")]
    )
    session = lsp.LspSession(tmp_path, lsp.ServerSpec(("fixture",), "python"))
    monkeypatch.setattr(session, "notify", AsyncMock())
    locations = [
        {"uri": path.as_uri(), "range": {"start": {"line": 0, "character": 0}}}
        for path in [private, public]
    ]
    monkeypatch.setattr(session, "request", AsyncMock(return_value=locations))
    read_text = Path.read_text

    def guarded_read(path: Path, *args, **kwargs):
        assert path != private, "denied reference contents were read"
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    results = await session.references(
        public,
        line=1,
        character=0,
        limit=1,
        glob=None,
        path_filter=lambda path: fs.read_allowed(context, path, "refs"),
    )
    assert results == ["public.py:1: target()"]


@pytest.mark.parametrize("tool", [GlobTool(), GrepTool(), RepoMapTool(), SymbolsTool(), RefsTool()])
async def test_search_filters_symlink_escape(tmp_path: Path, monkeypatch, tool):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SIWAH_LSP", "off")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("def outside_sentinel(): pass\n", encoding="utf-8")
    try:
        (workspace / "link.py").symlink_to(outside)
    except OSError:
        pytest.skip("creating symlinks requires unavailable OS privileges")
    result = await tool.run(
        context_for(workspace),
        pattern="*" if tool.name == "glob" else "sentinel",
        query="sentinel",
    )
    assert result.ok, result.error
    assert "outside_sentinel" not in result.output
    assert "link.py" not in result.output


@pytest.mark.parametrize("tool", [GlobTool(), GrepTool(), RepoMapTool(), SymbolsTool(), RefsTool()])
async def test_search_filters_mock_symlink_resolution(tmp_path: Path, monkeypatch, tool):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SIWAH_LSP", "off")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    candidate = workspace / "link.py"
    candidate.write_text("def outside_sentinel(): pass\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    resolve = Path.resolve

    def resolve_link(path: Path, *args, **kwargs):
        return outside if path == candidate else resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_link)
    result = await tool.run(
        context_for(workspace),
        pattern="*" if tool.name == "glob" else "sentinel",
        query="sentinel",
    )
    assert result.ok, result.error
    assert "outside_sentinel" not in result.output
    assert "link.py" not in result.output


@pytest.mark.parametrize("tool", [GlobTool(), GrepTool(), RepoMapTool(), SymbolsTool(), RefsTool()])
async def test_search_filters_denied_directory(tmp_path: Path, monkeypatch, tool):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SIWAH_LSP", "off")
    private = tmp_path / "private"
    private.mkdir()
    (private / "module.py").write_text("def hidden_sentinel(): pass\n", encoding="utf-8")
    context = context_for(
        tmp_path, [PermissionRule(tool="read_file", path="private", decision="deny")]
    )
    result = await tool.run(
        context, pattern="**/*.py" if tool.name == "glob" else "sentinel", query="sentinel"
    )
    assert result.ok, result.error
    assert "hidden_sentinel" not in result.output
    assert "module.py" not in result.output


@pytest.mark.parametrize("operation", ["write", "edit"])
async def test_commit_rechecks_bytes_after_temporary_write(tmp_path: Path, monkeypatch, operation):
    path = tmp_path / "note.txt"
    path.write_bytes(b"before\r\n")
    atomic_write = fs._atomic_write

    def race(target: Path, content: str, **kwargs: Any) -> None:
        validate = kwargs["validate"]
        checks = 0

        def validate_with_race():
            nonlocal checks
            validate()
            checks += 1
            if checks == 2:
                # Simulate another writer while the temporary file was prepared.
                path.write_bytes(b"human\r\n")

        atomic_write(target, content, expected=kwargs["expected"], validate=validate_with_race)

    monkeypatch.setattr(fs, "_atomic_write", race)
    context = context_for(tmp_path)
    if operation == "edit":
        result = await EditFileTool().run(
            context, path="note.txt", old_string="before", new_string="agent"
        )
    else:
        result = await WriteFileTool().run(context, path="note.txt", content="agent\n")
    assert not result.ok
    assert "stale" in (result.error or "").lower()
    assert path.read_bytes() == b"human\r\n"
