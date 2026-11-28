"""Managed LSP reference transport with a safe fallback boundary.

The transport is deliberately small: start a detected language server, open one
document, ask for references, and close it. Any failure returns ``None`` so the
caller can use the git grep/ripgrep fallback.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .index import WorkspaceIndex, get_index

DEFAULT_TIMEOUT = 20.0


class LspError(RuntimeError):
    pass


@dataclass(frozen=True)
class ServerSpec:
    command: tuple[str, ...]
    language_id: str


def _candidate(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    executable = Path(sys.executable)
    for suffix in ("", ".exe", ".cmd", ".bat"):
        path = executable.parent / f"{name}{suffix}"
        if path.is_file():
            return str(path)
    return None


def server_for(path: Path) -> ServerSpec | None:
    suffix = path.suffix.lower()
    if suffix in {".py", ".pyi"}:
        executable = _candidate("pyright-langserver") or _candidate("pyright-python-langserver")
        return ServerSpec((executable, "--stdio"), "python") if executable else None
    if suffix in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
        executable = _candidate("typescript-language-server")
        language = {
            ".ts": "typescript",
            ".tsx": "typescriptreact",
            ".js": "javascript",
            ".jsx": "javascriptreact",
            ".mjs": "javascript",
            ".cjs": "javascript",
        }[suffix]
        return ServerSpec((executable, "--stdio"), language) if executable else None
    if suffix == ".go":
        executable = _candidate("gopls")
        return ServerSpec((executable,), "go") if executable else None
    if suffix == ".rs":
        executable = _candidate("rust-analyzer")
        return ServerSpec((executable,), "rust") if executable else None
    if suffix in {".c", ".h", ".cc", ".cpp", ".hpp"}:
        executable = _candidate("clangd")
        return (
            ServerSpec((executable,), "c" if suffix in {".c", ".h"} else "cpp")
            if executable
            else None
        )
    return None


class LspSession:
    def __init__(self, workspace: Path, spec: ServerSpec) -> None:
        self.workspace = workspace
        self.spec = spec
        self.process: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._buffer = b""

    async def __aenter__(self) -> LspSession:
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.spec.command,
                cwd=str(self.workspace),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            raise LspError(f"Cannot start {self.spec.command[0]}: {exc}") from exc
        try:
            await self.request(
                "initialize",
                {
                    "processId": os.getpid(),
                    "rootUri": self.workspace.as_uri(),
                    "capabilities": {},
                    "workspaceFolders": [
                        {"uri": self.workspace.as_uri(), "name": self.workspace.name}
                    ],
                },
                timeout=30.0,
            )
            await self.notify("initialized", {})
        except Exception:
            await self.aclose()
            raise
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            if process.returncode is None:
                # request()/notify() require self.process, so it must stay set
                # until the graceful shutdown handshake has been written.
                with contextlib.suppress(OSError, LspError, TimeoutError):
                    await self.request("shutdown", {}, timeout=1.0)
                with contextlib.suppress(OSError, LspError):
                    await self.notify("exit", {})
                try:
                    await asyncio.wait_for(process.wait(), timeout=1.0)
                except (TimeoutError, OSError):
                    _kill_tree(process)
                    with contextlib.suppress(TimeoutError, OSError):
                        await asyncio.wait_for(process.wait(), timeout=2.0)
        finally:
            self.process = None
            if process.stdin is not None:
                with contextlib.suppress(OSError):
                    process.stdin.close()

    def _write(self, payload: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise LspError("LSP process is not running")
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.process.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)

    async def _flush(self) -> None:
        if self.process is None or self.process.stdin is None:
            raise LspError("LSP process is not running")
        await self.process.stdin.drain()

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})
        await self._flush()

    async def _read(self) -> dict[str, Any]:
        if self.process is None or self.process.stdout is None:
            raise LspError("LSP process is not running")
        while True:
            header = await self.process.stdout.readuntil(b"\r\n\r\n")
            length = None
            for line in header.decode("ascii", "replace").splitlines():
                if line.lower().startswith("content-length:"):
                    length = int(line.split(":", 1)[1].strip())
                    break
            if length is None:
                raise LspError("LSP message has no Content-Length")
            body = await self.process.stdout.readexactly(length)
            return json.loads(body.decode("utf-8", "replace"))

    async def request(
        self, method: str, params: dict[str, Any], *, timeout: float = DEFAULT_TIMEOUT
    ) -> Any:
        request_id = self._next_id
        self._next_id += 1
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        await self._flush()
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"LSP {method} timed out")
            message = await asyncio.wait_for(self._read(), timeout=remaining)
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise LspError(str(message["error"]))
                return message.get("result")
            if "id" in message and "method" in message:
                # Server-to-client request (progress, configuration, etc.).
                self._write({"jsonrpc": "2.0", "id": message["id"], "result": None})
                await self._flush()

    async def references(
        self,
        path: Path,
        *,
        line: int,
        character: int,
        limit: int,
        glob: str | None,
        path_filter: Callable[[Path], bool] | None = None,
    ) -> list[str]:
        if path_filter is not None and not path_filter(path):
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
        uri = path.as_uri()
        await self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": self.spec.language_id,
                    "version": 1,
                    "text": text,
                }
            },
        )
        try:
            result = await self.request(
                "textDocument/references",
                {
                    "textDocument": {"uri": uri},
                    "position": {"line": max(0, line - 1), "character": max(0, character)},
                    "context": {"includeDeclaration": True},
                },
                timeout=30.0,
            )
        finally:
            await self.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
        if not isinstance(result, list):
            return []
        results: list[str] = []
        for item in result:
            if not isinstance(item, dict):
                continue
            location = _uri_to_path(str(item.get("uri", "")))
            if location is None:
                continue
            try:
                relative = location.resolve().relative_to(self.workspace).as_posix()
            except (OSError, ValueError):
                continue
            if glob and not _glob_match(relative, glob):
                continue
            if path_filter is not None and not path_filter(location):
                continue
            start = item.get("range", {}).get("start", {})
            number = int(start.get("line", 0)) + 1
            try:
                content = (
                    location.read_text(encoding="utf-8", errors="replace")
                    .splitlines()[number - 1]
                    .strip()[:300]
                )
            except (OSError, IndexError):
                content = ""
            results.append(f"{relative}:{number}: {content}")
            if len(results) >= max(1, limit):
                break
        return results

    async def _open(self, path: Path) -> str:
        text = path.read_text(encoding="utf-8", errors="replace")
        uri = path.as_uri()
        await self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": self.spec.language_id,
                    "version": 1,
                    "text": text,
                }
            },
        )
        return uri

    async def _close(self, uri: str) -> None:
        await self.notify("textDocument/didClose", {"textDocument": {"uri": uri}})

    async def definition(self, path: Path, *, line: int, character: int) -> list[dict[str, Any]]:
        uri = await self._open(path)
        try:
            result = await self.request(
                "textDocument/definition",
                {
                    "textDocument": {"uri": uri},
                    "position": {"line": max(0, line - 1), "character": max(0, character)},
                },
                timeout=30.0,
            )
        finally:
            await self._close(uri)
        if isinstance(result, dict):
            return [result]
        return (
            [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []
        )

    async def hover(self, path: Path, *, line: int, character: int) -> str:
        uri = await self._open(path)
        try:
            result = await self.request(
                "textDocument/hover",
                {
                    "textDocument": {"uri": uri},
                    "position": {"line": max(0, line - 1), "character": max(0, character)},
                },
                timeout=30.0,
            )
        finally:
            await self._close(uri)
        if not isinstance(result, dict):
            return ""
        contents = result.get("contents")
        if isinstance(contents, str):
            return contents
        if isinstance(contents, dict):
            return str(contents.get("value") or "")
        if isinstance(contents, list):
            parts: list[str] = []
            for item in contents:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.append(str(item.get("value") or ""))
            return "\n".join(part for part in parts if part)
        return ""

    async def diagnostics(self, path: Path, *, timeout: float = 10.0) -> list[dict[str, Any]]:
        uri = await self._open(path)
        try:
            # LSP 3.17 pull diagnostics. Pyright and many modern servers support
            # this and return the final result without waiting for a publish.
            try:
                result = await self.request(
                    "textDocument/diagnostic",
                    {"textDocument": {"uri": uri}},
                    timeout=timeout,
                )
                if isinstance(result, dict) and isinstance(result.get("items"), list):
                    return [item for item in result["items"] if isinstance(item, dict)]
            except (LspError, TimeoutError):
                pass
            found: list[dict[str, Any]] = []
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    message = await asyncio.wait_for(self._read(), timeout=remaining)
                except (TimeoutError, asyncio.IncompleteReadError):
                    break
                if message.get("method") == "textDocument/publishDiagnostics":
                    params = message.get("params") or {}
                    if params.get("uri") == uri:
                        values = params.get("diagnostics")
                        found = (
                            [item for item in values if isinstance(item, dict)]
                            if isinstance(values, list)
                            else []
                        )
                        break
                    continue
                if "id" in message and "method" in message:
                    self._write({"jsonrpc": "2.0", "id": message["id"], "result": None})
                    await self._flush()
            return found
        finally:
            await self._close(uri)

    async def rename(
        self, path: Path, *, line: int, character: int, new_name: str
    ) -> dict[str, Any] | None:
        uri = await self._open(path)
        try:
            result = await self.request(
                "textDocument/rename",
                {
                    "textDocument": {"uri": uri},
                    "position": {"line": max(0, line - 1), "character": max(0, character)},
                    "newName": new_name,
                },
                timeout=30.0,
            )
        finally:
            await self._close(uri)
        return result if isinstance(result, dict) else None


def _kill_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    else:
        process.kill()


def _uri_to_path(uri: str) -> Path | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    value = unquote(parsed.path)
    if os.name == "nt" and value.startswith("/") and len(value) > 2 and value[2] == ":":
        value = value[1:]
    return Path(value) if value else None


def _glob_match(relative: str, pattern: str) -> bool:
    from fnmatch import fnmatch

    return fnmatch(relative, pattern) or fnmatch(Path(relative).name, pattern)


async def find_references_lsp(
    workspace: Path,
    query: str,
    *,
    limit: int = 100,
    glob: str | None = None,
    index: WorkspaceIndex | None = None,
    path_filter: Callable[[Path], bool] | None = None,
) -> list[str] | None:
    """Return LSP references, or None when LSP cannot serve the request."""

    if os.environ.get("SIWAH_LSP", "auto").lower() == "off":
        return None
    if not query.strip() or not query.replace("_", "").isalnum():
        return None
    workspace = workspace.resolve()
    index = index or get_index(workspace)
    matches = index.find_symbols(query, limit=5, path_filter=path_filter, exact_name=True)
    if not matches:
        return None
    for relative, symbol in matches:
        if symbol.name.split(".")[-1] != query:
            continue
        path = workspace / relative
        if path_filter is not None and not path_filter(path):
            continue
        spec = server_for(path)
        if spec is None:
            continue
        try:
            source_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            text = source_lines[symbol.line - 1] if symbol.line > 0 else ""
        except (OSError, IndexError):
            continue
        match = re.search(rf"(?<!\w){re.escape(query)}(?!\w)", text)
        if match is None:
            continue
        character = len(text[: match.start()].encode("utf-16-le")) // 2
        try:
            async with LspSession(workspace, spec) as session:
                return await session.references(
                    path,
                    line=symbol.line,
                    character=character,
                    limit=limit,
                    glob=glob,
                    path_filter=path_filter,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
    return None


__all__ = ["LspError", "LspSession", "ServerSpec", "find_references_lsp", "server_for"]
