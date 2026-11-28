"""Language-server tools: diagnostics, definition, hover, and rename."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from ..permissions.policy import PermissionRequest
from ..repo.lsp import LspError, LspSession, server_for
from .base import Tool, ToolContext, ToolResult, integer_property, object_schema, string_property
from .fs import read_allowed

_SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}


def _path(context: ToolContext, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = context.workspace / path
    return path.expanduser().resolve()


def _uri_path(uri: str) -> Path | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    value = unquote(parsed.path)
    if os.name == "nt" and value.startswith("/") and len(value) > 2 and value[2] == ":":
        value = value[1:]
    return Path(value) if value else None


def _relative(workspace: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(workspace).as_posix()
    except (OSError, ValueError):
        return str(path)


def _line_text(path: Path, line: int) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[line - 1].strip()[:300] if 0 < line <= len(lines) else ""
    except OSError:
        return ""


def _format_locations(workspace: Path, items: list[dict[str, Any]], limit: int = 50) -> list[str]:
    rows: list[str] = []
    for item in items:
        uri = str(item.get("uri") or "")
        path = _uri_path(uri)
        if path is None:
            continue
        start = (item.get("range") or {}).get("start") or {}
        line = int(start.get("line", 0)) + 1
        character = int(start.get("character", 0)) + 1
        rows.append(f"{_relative(workspace, path)}:{line}:{character}: {_line_text(path, line)}")
        if len(rows) >= limit:
            break
    return rows


def _offset(text: str, line: int, character: int) -> int:
    lines = text.splitlines(keepends=True)
    if line < 0 or line >= len(lines):
        raise ValueError("edit line is outside the file")
    base = sum(len(part) for part in lines[:line])
    segment = lines[line]
    units = 0
    for index, char in enumerate(segment):
        if units >= character:
            return base + index
        units += 2 if ord(char) > 0xFFFF else 1
    return base + len(segment)


def _apply_edits(text: str, edits: list[dict[str, Any]]) -> str:
    ranges: list[tuple[int, int, str]] = []
    for edit in edits:
        range_value = edit.get("range") or {}
        start = range_value.get("start") or {}
        end = range_value.get("end") or {}
        start_offset = _offset(text, int(start.get("line", 0)), int(start.get("character", 0)))
        end_offset = _offset(text, int(end.get("line", 0)), int(end.get("character", 0)))
        if end_offset < start_offset:
            raise ValueError("edit range is reversed")
        ranges.append((start_offset, end_offset, str(edit.get("newText") or "")))
    ranges.sort(key=lambda item: (item[0], item[1]))
    for previous, current in zip(ranges, ranges[1:], strict=False):
        if current[0] < previous[1]:
            raise ValueError("workspace edit contains overlapping ranges")
    for start_offset, end_offset, replacement in reversed(ranges):
        text = text[:start_offset] + replacement + text[end_offset:]
    return text


def _edit_files(workspace: Path, edit: dict[str, Any]) -> dict[Path, list[dict[str, Any]]]:
    files: dict[Path, list[dict[str, Any]]] = {}
    changes = edit.get("changes")
    if isinstance(changes, dict):
        for uri, edits in changes.items():
            path = _uri_path(str(uri))
            if path is not None and isinstance(edits, list):
                files.setdefault(path.resolve(), []).extend(
                    item for item in edits if isinstance(item, dict)
                )
    document_changes = edit.get("documentChanges")
    if isinstance(document_changes, list):
        for change in document_changes:
            if not isinstance(change, dict) or "textDocument" not in change:
                continue
            document = change.get("textDocument") or {}
            path = _uri_path(str(document.get("uri") or ""))
            edits = change.get("edits")
            if path is not None and isinstance(edits, list):
                files.setdefault(path.resolve(), []).extend(
                    item for item in edits if isinstance(item, dict)
                )
    return files


def _atomic_write(path: Path, text: str) -> None:
    mode = path.stat().st_mode if path.exists() else 0o644
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.chmod(temp, mode)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _server(path: Path):
    if os.environ.get("SIWAH_LSP", "auto").lower() == "off":
        return None
    return server_for(path)


class LspDiagnosticsTool(Tool):
    name = "lsp_diagnostics"
    description = "Return language-server diagnostics for one file."
    kind = "read"
    parameters = object_schema(
        {
            "path": string_property("File path relative to the workspace."),
            "timeout_s": integer_property("Seconds to wait for diagnostics.", 10),
        },
        ["path"],
    )

    async def run(self, context: ToolContext, **arguments: Any) -> ToolResult:
        path = _path(context, str(arguments.get("path", "")))
        if not path.is_file() or not read_allowed(context, path, self.name):
            return ToolResult.failure(f"File is unavailable: {path}")
        allowed, reason = await context.authorize(
            PermissionRequest(tool=self.name, kind="read", path=str(path), preview=str(path))
        )
        if not allowed:
            return ToolResult.failure(reason or "Diagnostics denied")
        spec = _server(path)
        if spec is None:
            return ToolResult.failure("No language server is available for this file")
        try:
            async with LspSession(context.workspace, spec) as session:
                values = await session.diagnostics(
                    path, timeout=min(30.0, max(1.0, float(arguments.get("timeout_s", 10))))
                )
        except asyncio.CancelledError:
            raise
        except (LspError, OSError, TimeoutError) as exc:
            return ToolResult.failure(f"LSP diagnostics failed: {exc}")
        if not values:
            return ToolResult.success("(no diagnostics)")
        lines = []
        for item in values[:100]:
            start = (item.get("range") or {}).get("start") or {}
            line = int(start.get("line", 0)) + 1
            character = int(start.get("character", 0)) + 1
            severity = _SEVERITY.get(int(item.get("severity", 1)), "info")
            source = f" [{item.get('source')}]" if item.get("source") else ""
            lines.append(
                f"{_relative(context.workspace, path)}:{line}:{character}: "
                f"{severity}{source}: {item.get('message', '')}"
            )
        return ToolResult.success("\n".join(lines), data={"count": len(values)})


class LspDefinitionTool(Tool):
    name = "lsp_definition"
    description = "Find the definition of a symbol at a file position."
    kind = "read"
    parameters = object_schema(
        {
            "path": string_property("File path relative to the workspace."),
            "line": integer_property("One-based line number.", 1),
            "column": integer_property("One-based column number.", 1),
        },
        ["path", "line"],
    )

    async def run(self, context: ToolContext, **arguments: Any) -> ToolResult:
        path = _path(context, str(arguments.get("path", "")))
        if not path.is_file() or not read_allowed(context, path, self.name):
            return ToolResult.failure(f"File is unavailable: {path}")
        allowed, reason = await context.authorize(
            PermissionRequest(tool=self.name, kind="read", path=str(path), preview=str(path))
        )
        if not allowed:
            return ToolResult.failure(reason or "Definition lookup denied")
        spec = _server(path)
        if spec is None:
            return ToolResult.failure("No language server is available for this file")
        try:
            async with LspSession(context.workspace, spec) as session:
                values = await session.definition(
                    path,
                    line=max(1, int(arguments.get("line", 1))),
                    character=max(0, int(arguments.get("column", 1)) - 1),
                )
        except asyncio.CancelledError:
            raise
        except (LspError, OSError, TimeoutError) as exc:
            return ToolResult.failure(f"LSP definition failed: {exc}")
        rows = _format_locations(context.workspace, values)
        return ToolResult.success("\n".join(rows) or "(no definition)", data={"count": len(rows)})


class LspHoverTool(Tool):
    name = "lsp_hover"
    description = "Show type/documentation information for a symbol at a file position."
    kind = "read"
    parameters = object_schema(
        {
            "path": string_property("File path relative to the workspace."),
            "line": integer_property("One-based line number.", 1),
            "column": integer_property("One-based column number.", 1),
        },
        ["path", "line"],
    )

    async def run(self, context: ToolContext, **arguments: Any) -> ToolResult:
        path = _path(context, str(arguments.get("path", "")))
        if not path.is_file() or not read_allowed(context, path, self.name):
            return ToolResult.failure(f"File is unavailable: {path}")
        allowed, reason = await context.authorize(
            PermissionRequest(tool=self.name, kind="read", path=str(path), preview=str(path))
        )
        if not allowed:
            return ToolResult.failure(reason or "Hover lookup denied")
        spec = _server(path)
        if spec is None:
            return ToolResult.failure("No language server is available for this file")
        try:
            async with LspSession(context.workspace, spec) as session:
                text = await session.hover(
                    path,
                    line=max(1, int(arguments.get("line", 1))),
                    character=max(0, int(arguments.get("column", 1)) - 1),
                )
        except asyncio.CancelledError:
            raise
        except (LspError, OSError, TimeoutError) as exc:
            return ToolResult.failure(f"LSP hover failed: {exc}")
        return ToolResult.success(text or "(no hover information)")


class LspRenameTool(Tool):
    name = "lsp_rename"
    description = (
        "Preview or apply a language-server rename. Preview by default; applying requires "
        "write permission and updates every returned file atomically."
    )
    kind = "write"
    parameters = object_schema(
        {
            "path": string_property("File path relative to the workspace."),
            "line": integer_property("One-based line number.", 1),
            "column": integer_property("One-based column number.", 1),
            "new_name": string_property("New symbol name."),
            "apply": {
                "type": "boolean",
                "default": False,
                "description": "Apply the edits; false only previews them.",
            },
        },
        ["path", "line", "new_name"],
    )

    async def run(self, context: ToolContext, **arguments: Any) -> ToolResult:
        path = _path(context, str(arguments.get("path", "")))
        new_name = str(arguments.get("new_name", "")).strip()
        if not path.is_file() or not read_allowed(context, path, self.name):
            return ToolResult.failure(f"File is unavailable: {path}")
        if not new_name or not new_name.replace("_", "").isalnum():
            return ToolResult.failure("new_name must be a valid identifier")
        spec = _server(path)
        if spec is None:
            return ToolResult.failure("No language server is available for this file")
        try:
            async with LspSession(context.workspace, spec) as session:
                edit = await session.rename(
                    path,
                    line=max(1, int(arguments.get("line", 1))),
                    character=max(0, int(arguments.get("column", 1)) - 1),
                    new_name=new_name,
                )
        except asyncio.CancelledError:
            raise
        except (LspError, OSError, TimeoutError) as exc:
            return ToolResult.failure(f"LSP rename failed: {exc}")
        if not edit:
            return ToolResult.failure("The language server returned no rename edits")
        files = _edit_files(context.workspace, edit)
        if not files:
            return ToolResult.failure("The rename produced no workspace files")
        workspace = context.workspace.resolve()
        for target in files:
            if not target.is_relative_to(workspace):
                return ToolResult.failure(f"Rename escapes the workspace: {target}")
        preview = "\n".join(
            f"{_relative(context.workspace, target)}: {len(edits)} edit(s)"
            for target, edits in sorted(files.items())
        )
        if not bool(arguments.get("apply", False)):
            return ToolResult.success(
                f"Rename preview for {new_name}:\n{preview}\n"
                "Re-run with apply=true after reviewing the affected files.",
                data={"files": [_relative(context.workspace, target) for target in files]},
            )
        allowed, reason = await context.authorize(
            PermissionRequest(
                tool=self.name,
                kind="write",
                path=str(context.workspace),
                preview=f"Rename to {new_name}:\n{preview}",
            )
        )
        if not allowed:
            return ToolResult.failure(reason or "Rename denied")
        changed: list[str] = []
        try:
            for target, edits in files.items():
                text = target.read_text(encoding="utf-8", errors="replace")
                _atomic_write(target, _apply_edits(text, edits))
                changed.append(_relative(context.workspace, target))
        except (OSError, ValueError) as exc:
            return ToolResult.failure(f"Could not apply rename: {exc}")
        return ToolResult.success(
            f"Renamed symbol to {new_name} in {len(changed)} file(s): " + ", ".join(changed),
            data={"files": changed},
        )


__all__ = ["LspDefinitionTool", "LspDiagnosticsTool", "LspHoverTool", "LspRenameTool"]
