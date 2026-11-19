"""Filesystem, edit, and search tools."""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import os
import re
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..paths import harness_dir
from ..permissions.policy import PathGuardError, PermissionRequest
from .base import (
    Tool,
    ToolContext,
    ToolResult,
    boolean_property,
    integer_property,
    object_schema,
    string_property,
)
from .syntax import validate_syntax

_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "__pycache__",
}
_MUTATION_LOCK = threading.Lock()


class ReadFileTool(Tool):
    name = "read_file"
    description = "Read a text file from the workspace. Returns numbered lines."
    kind = "read"
    parameters = object_schema(
        {
            "path": string_property(
                "Path relative to the workspace, or an absolute workspace path."
            ),
            "offset": integer_property("1-based first line to return.", 1),
            "limit": integer_property("Maximum lines to return.", 2000),
        },
        ["path"],
    )

    async def run(self, context: ToolContext, **arguments: Any) -> ToolResult:
        path = context.policy.resolve(arguments["path"], write=False)
        allowed, reason = await context.authorize(
            PermissionRequest(
                tool=self.name,
                kind="read",
                path=str(path),
                preview=f"Read {path}",
            )
        )
        if not allowed:
            return ToolResult.failure(reason or "Read denied")
        if not path.is_file():
            return ToolResult.failure(f"File not found: {arguments['path']}")
        raw = path.read_bytes()
        context.read_hashes[str(path)] = hashlib.sha256(raw).hexdigest()
        text = raw.decode("utf-8", errors="replace")
        offset = max(int(arguments.get("offset", 1)), 1)
        limit = min(max(int(arguments.get("limit", 2000)), 1), 20_000)
        lines = text.splitlines()
        selected = lines[offset - 1 : offset - 1 + limit]
        output = "\n".join(f"{offset + index:>5}  {line}" for index, line in enumerate(selected))
        if not selected:
            output = "(empty or offset beyond end of file)"
        return ToolResult.success(output, data={"path": str(path), "lines": len(lines)})


class WriteFileTool(Tool):
    name = "write_file"
    description = "Create or replace a text file in the workspace. Writes are atomic."
    kind = "write"
    parameters = object_schema(
        {
            "path": string_property("Path relative to the workspace."),
            "content": string_property("Full file content."),
        },
        ["path", "content"],
    )

    async def run(self, context: ToolContext, **arguments: Any) -> ToolResult:
        path = context.policy.resolve(arguments["path"], write=True)
        content = str(arguments["content"])
        raw = _file_bytes(path)
        expected = context.read_hashes.get(str(path))
        if expected and (raw is None or expected != hashlib.sha256(raw).hexdigest()):
            return ToolResult.failure(_stale_message(path))
        syntax_error = validate_syntax(path, content)
        if syntax_error:
            return ToolResult.failure(f"Refusing to write invalid syntax: {syntax_error}")
        allowed, reason = await context.authorize(
            PermissionRequest(
                tool=self.name,
                kind="write",
                path=str(path),
                preview=f"Write {path} ({len(content)} characters)",
            )
        )
        if not allowed:
            return ToolResult.failure(reason or "Write denied")
        error = _commit_file(context, self.name, arguments["path"], path, content, raw)
        if error:
            return ToolResult.failure(error)
        previous = raw.decode("utf-8", errors="replace") if raw is not None else None
        context.read_hashes[str(path)] = hashlib.sha256(content.encode()).hexdigest()
        patch = (
            display_diff(path, previous, content)
            if previous is not None and previous != content
            else None
        )
        # The client shows the whole file for a new file and a diff when an
        # existing file was replaced; the model gets the short status line.
        return ToolResult.success(
            f"Wrote {len(content)} characters to {path}",
            data={
                "full_output": content,
                "edited": patch is not None,
                **({"diff": patch} if patch is not None else {}),
            },
        )


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "Edit a workspace file with exact or whitespace-fuzzy search/replace, a unified "
        "diff, or a whole-file replacement. Fails instead of guessing when the target "
        "is missing, ambiguous, or the file changed since it was read."
    )
    kind = "write"
    parameters = object_schema(
        {
            "path": string_property("Path relative to the workspace."),
            "old_string": string_property("Exact text to replace (search/replace)."),
            "new_string": string_property("Replacement text (search/replace)."),
            "replace_all": boolean_property("Replace every occurrence.", False),
            "patch": string_property("Unified diff to apply instead of search/replace."),
            "content": string_property("Full replacement content for a whole-file edit."),
            "format": {
                "type": "string",
                "enum": ["auto", "search_replace", "unified_diff", "whole_file"],
                "default": "auto",
                "description": "Edit format; auto selects from the provided arguments.",
            },
        },
        ["path"],
    )

    async def run(self, context: ToolContext, **arguments: Any) -> ToolResult:
        path = context.policy.resolve(arguments["path"], write=True)
        if not path.is_file():
            return ToolResult.failure(f"File not found: {arguments['path']}")
        raw = path.read_bytes()
        try:
            original = raw.decode("utf-8")
        except UnicodeDecodeError:
            return ToolResult.failure(f"Refusing to edit {path}: the file is not valid UTF-8")
        crlf = "\r\n" in original
        current = original.replace("\r\n", "\n") if crlf else original
        expected = context.read_hashes.get(str(path))
        actual = hashlib.sha256(raw).hexdigest()
        if expected and expected != actual:
            return ToolResult.failure(
                f"Refusing stale edit: {path} changed after it was read. Re-read the file."
            )

        requested = str(arguments.get("format", "auto"))
        patch = arguments.get("patch")
        content = arguments.get("content")
        old = arguments.get("old_string")
        new = arguments.get("new_string")
        if crlf:
            patch = patch.replace("\r\n", "\n") if isinstance(patch, str) else patch
            old = old.replace("\r\n", "\n") if isinstance(old, str) else old
            new = new.replace("\r\n", "\n") if isinstance(new, str) else new
        if requested == "auto":
            if patch is not None:
                requested = "unified_diff"
            elif content is not None and old is None:
                requested = "whole_file"
            else:
                requested = "search_replace"

        try:
            if requested == "unified_diff":
                if not isinstance(patch, str) or not patch.strip():
                    return ToolResult.failure("patch is required for a unified_diff edit")
                updated = _apply_unified_patch(current, patch)
                replacements = None
            elif requested == "whole_file":
                if not isinstance(content, str):
                    return ToolResult.failure("content is required for a whole_file edit")
                updated = content
                replacements = None
            elif requested == "search_replace":
                if not isinstance(old, str) or not isinstance(new, str):
                    return ToolResult.failure(
                        "old_string and new_string are required for a search_replace edit"
                    )
                if not old:
                    return ToolResult.failure("old_string must not be empty")
                updated, replacements = _search_replace(
                    current, old, new, bool(arguments.get("replace_all", False))
                )
            else:
                return ToolResult.failure(f"Unknown edit format: {requested}")
        except ValueError as exc:
            return ToolResult.failure(str(exc))

        if crlf and requested != "whole_file":
            updated = updated.replace("\n", "\r\n")
        syntax_error = validate_syntax(path, updated)
        if syntax_error:
            return ToolResult.failure(f"Refusing to write invalid syntax: {syntax_error}")

        allowed, reason = await context.authorize(
            PermissionRequest(
                tool=self.name,
                kind="write",
                path=str(path),
                preview=f"Edit {path} ({requested})",
            )
        )
        if not allowed:
            return ToolResult.failure(reason or "Edit denied")
        error = _commit_file(context, self.name, arguments["path"], path, updated, raw)
        if error:
            return ToolResult.failure(error)
        context.read_hashes[str(path)] = hashlib.sha256(updated.encode()).hexdigest()
        return ToolResult.success(
            f"Edited {path} with {requested}",
            data={
                "format": requested,
                "replacements": replacements,
                "diff": display_diff(path, current, updated),
            },
        )


def display_diff(path: Path, before: str, after: str) -> str:
    diff = difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        fromfile=f"a/{path.name}",
        tofile=f"b/{path.name}",
        lineterm="",
    )
    return "\n".join(diff)


def _search_replace(current: str, old: str, new: str, replace_all: bool) -> tuple[str, int | None]:
    count = current.count(old)
    if count > 0:
        if count > 1 and not replace_all:
            raise ValueError(
                f"old_string occurs {count} times; pass replace_all=true or use a unique snippet"
            )
        return (
            current.replace(old, new) if replace_all else current.replace(old, new, 1),
            count if replace_all else 1,
        )
    fuzzy = _fuzzy_replace(current, old, new, replace_all)
    if fuzzy is None:
        raise ValueError("old_string was not found in the file, even after whitespace matching")
    return fuzzy


def _fuzzy_replace(current: str, old: str, new: str, replace_all: bool) -> tuple[str, int] | None:
    old_lines = [line.strip() for line in old.splitlines()]
    if not old_lines:
        return None
    newline = "\r\n" if "\r\n" in current else "\n"
    trailing = current.endswith(("\n", "\r"))
    lines = current.splitlines()
    matches: list[int] = []
    for index in range(0, len(lines) - len(old_lines) + 1):
        window = [line.strip() for line in lines[index : index + len(old_lines)]]
        if window == old_lines:
            matches.append(index)
    if not matches:
        return None
    if len(matches) > 1 and not replace_all:
        raise ValueError(
            f"fuzzy old_string matches {len(matches)} locations; pass replace_all=true or "
            "use a more specific snippet"
        )
    replacement = new.splitlines()
    for index in reversed(matches):
        lines[index : index + len(old_lines)] = replacement
    result = newline.join(lines)
    if trailing:
        result += newline
    return result, len(matches)


def _apply_unified_patch(current: str, patch: str) -> str:
    newline = "\r\n" if "\r\n" in current else "\n"
    trailing = current.endswith(("\n", "\r"))
    old_lines = current.splitlines()
    patch_lines = patch.replace("\r\n", "\n").splitlines()
    index = 0
    hunks: list[tuple[int, int, list[str]]] = []
    while index < len(patch_lines):
        line = patch_lines[index]
        if line.startswith("@@"):
            match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if not match:
                raise ValueError(f"Invalid unified diff hunk header: {line}")
            old_start = int(match.group(1))
            old_count = int(match.group(2) or "1")
            body: list[str] = []
            index += 1
            while index < len(patch_lines) and not patch_lines[index].startswith("@@"):
                body.append(patch_lines[index])
                index += 1
            hunks.append((old_start, old_count, body))
            continue
        index += 1
    if not hunks:
        raise ValueError("No unified diff hunks found")

    result: list[str] = []
    cursor = 0
    for old_start, old_count, body in hunks:
        # For pure insertions the header names the line *after* the insertion
        # point (1-based), so no -1 adjustment applies.
        start = old_start - 1 if old_count else old_start
        if start < cursor or start > len(old_lines):
            raise ValueError("Unified diff hunks are out of order or out of range")
        result.extend(old_lines[cursor:start])
        position = start
        for line in body:
            if line.startswith("\\ No newline"):
                continue
            if not line:
                # An empty line in a patch is a context line with the prefix
                # stripped by some generators.
                if position < len(old_lines):
                    result.append(old_lines[position])
                    position += 1
                continue
            marker, value = line[0], line[1:]
            if marker == " ":
                if position >= len(old_lines) or old_lines[position] != value:
                    raise ValueError("Unified diff context does not match the file")
                result.append(old_lines[position])
                position += 1
            elif marker == "-":
                if position >= len(old_lines) or old_lines[position] != value:
                    raise ValueError("Unified diff removal does not match the file")
                position += 1
            elif marker == "+":
                result.append(value)
            else:
                raise ValueError(f"Invalid unified diff line: {line}")
        if position < start + old_count and old_count:
            raise ValueError("Unified diff hunk did not consume its old range")
        cursor = position
    result.extend(old_lines[cursor:])
    output = newline.join(result)
    if trailing:
        output += newline
    return output


class GlobTool(Tool):
    name = "glob"
    description = "Find files matching a glob pattern inside the workspace."
    kind = "read"
    parameters = object_schema(
        {
            "pattern": string_property("Glob pattern, for example **/*.py."),
            "path": string_property("Directory relative to the workspace.", "."),
            "limit": integer_property("Maximum results.", 200),
        },
        ["pattern"],
    )

    async def run(self, context: ToolContext, **arguments: Any) -> ToolResult:
        root = context.policy.resolve(arguments.get("path", "."), write=False)
        allowed, reason = await context.authorize(
            PermissionRequest(tool=self.name, kind="read", path=str(root), preview=f"Search {root}")
        )
        if not allowed:
            return ToolResult.failure(reason or "Search denied")
        if not root.is_dir():
            return ToolResult.failure(f"Not a directory: {root}")
        limit = min(max(int(arguments.get("limit", 200)), 1), 2000)
        matches: list[str] = []
        for candidate in root.glob(arguments["pattern"]):
            if any(part in _SKIP_DIRS for part in candidate.parts):
                continue
            if not read_allowed(context, candidate, self.name):
                continue
            if candidate.is_file():
                matches.append(str(candidate.relative_to(context.workspace)))
            if len(matches) >= limit:
                break
        matches.sort()
        return ToolResult.success(
            "\n".join(matches) or "(no matches)", data={"count": len(matches)}
        )


class GrepTool(Tool):
    name = "grep"
    description = "Search workspace text files with a Python regular expression."
    kind = "read"
    parameters = object_schema(
        {
            "pattern": string_property("Python regular expression."),
            "path": string_property("Directory or file to search.", "."),
            "glob": string_property("Optional file glob, for example *.py.", "*"),
            "case_sensitive": boolean_property("Case-sensitive matching.", True),
            "max_results": integer_property("Maximum matching lines.", 200),
        },
        ["pattern"],
    )

    async def run(self, context: ToolContext, **arguments: Any) -> ToolResult:
        root = context.policy.resolve(arguments.get("path", "."), write=False)
        allowed, reason = await context.authorize(
            PermissionRequest(tool=self.name, kind="read", path=str(root), preview=f"Search {root}")
        )
        if not allowed:
            return ToolResult.failure(reason or "Search denied")
        try:
            flags = 0 if arguments.get("case_sensitive", True) else re.IGNORECASE
            expression = re.compile(arguments["pattern"], flags)
        except re.error as exc:
            return ToolResult.failure(f"Invalid regular expression: {exc}")
        glob = arguments.get("glob", "*")
        max_results = min(max(int(arguments.get("max_results", 200)), 1), 2000)
        results: list[str] = []
        for path in _iter_files(root):
            if not read_allowed(context, path, self.name):
                continue
            if not _matches_glob(path, glob):
                continue
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw:
                continue
            text = raw.decode("utf-8", errors="replace")
            for number, line in enumerate(text.splitlines(), start=1):
                if expression.search(line):
                    results.append(f"{path.relative_to(context.workspace)}:{number}: {line}")
                    if len(results) >= max_results:
                        return ToolResult.success(
                            "\n".join(results), data={"count": len(results), "truncated": True}
                        )
        return ToolResult.success(
            "\n".join(results) or "(no matches)", data={"count": len(results)}
        )


def _iter_files(root: Path):
    if root.is_file():
        yield root
        return
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
        for filename in filenames:
            yield Path(directory) / filename


def _matches_glob(path: Path, pattern: str) -> bool:
    return fnmatch.fnmatch(path.name, pattern) or fnmatch.fnmatch(str(path), pattern)


def read_allowed(context: ToolContext, path: Path, tool: str) -> bool:
    """Filter leaves without prompting again after the search root was approved.

    Both the search tool and direct-read policy apply, including directory denies
    and symlink destinations. An ``ask`` is covered by the root approval.
    """
    try:
        resolved = context.policy.resolve(path, write=False)
    except (OSError, RuntimeError):
        return False
    roots = (context.policy.workspace, harness_dir().resolve())
    for candidate in {path, resolved}:
        root = next((root for root in roots if candidate.is_relative_to(root)), None)
        if root is None:
            return False
        for parent in (candidate, *candidate.parents):
            if any(
                context.policy.check(
                    PermissionRequest(tool=name, kind="read", path=str(parent), preview="")
                )
                == "deny"
                for name in {tool, "read_file"}
            ):
                return False
            if parent == root:
                break
    return True


def _file_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _stale_message(path: Path) -> str:
    return f"Refusing stale write: {path} changed after it was read. Re-read the file."


def _commit_file(
    context: ToolContext,
    tool: str,
    raw_path: str,
    path: Path,
    content: str,
    expected: bytes | None,
) -> str | None:
    def validate() -> None:
        if context.policy.resolve(raw_path, write=True) != path:
            raise ValueError(_stale_message(path))
        request = PermissionRequest(tool=tool, kind="write", path=str(path), preview="")
        if context.policy.check(request) == "deny":
            raise PathGuardError("Write denied by policy")

    try:
        # No await after this point: serialize cooperating writers and compare
        # the original bytes immediately before replacing the target.
        with _MUTATION_LOCK:
            _atomic_write(path, content, expected=expected, validate=validate)
    except (OSError, ValueError, PathGuardError) as exc:
        return str(exc)
    return None


def _atomic_write(
    path: Path, content: str, *, expected: bytes | None, validate: Callable[[], None]
) -> None:
    validate()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".siwah-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        validate()
        if _file_bytes(path) != expected:
            raise ValueError(_stale_message(path))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
