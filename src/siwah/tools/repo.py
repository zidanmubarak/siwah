"""Repo map, symbol search, and reference lookup tools."""

from __future__ import annotations

from typing import Any

from ..permissions.policy import PermissionRequest
from ..repo.index import get_index
from ..repo.lsp import find_references_lsp
from .base import Tool, ToolContext, ToolResult, integer_property, object_schema, string_property
from .fs import read_allowed


class RepoMapTool(Tool):
    name = "repo_map"
    description = (
        "Return a ranked map of the workspace's files and symbols. Use this "
        "before broad exploration."
    )
    kind = "read"
    parameters = object_schema(
        {
            "query": string_property("What you are looking for; empty returns a general map."),
        }
    )

    async def run(self, context: ToolContext, **arguments: Any) -> ToolResult:
        allowed, reason = await context.authorize(
            PermissionRequest(
                tool=self.name,
                kind="read",
                path=str(context.workspace),
                preview="Build a repository map",
            )
        )
        if not allowed:
            return ToolResult.failure(reason or "Repo map denied")
        budget = 32_000
        text = get_index(context.workspace).repo_map(
            str(arguments.get("query", "")),
            budget,
            path_filter=lambda path: read_allowed(context, path, self.name),
        )
        return ToolResult.success(text)


class SymbolsTool(Tool):
    name = "symbols"
    description = "Find definitions in the workspace by symbol name or path fragment."
    kind = "read"
    parameters = object_schema(
        {
            "query": string_property("Symbol name or path fragment."),
            "limit": integer_property("Maximum definitions to return.", 50),
        },
        ["query"],
    )

    async def run(self, context: ToolContext, **arguments: Any) -> ToolResult:
        allowed, reason = await context.authorize(
            PermissionRequest(
                tool=self.name,
                kind="read",
                path=str(context.workspace),
                preview=f"Find symbol {arguments.get('query', '')}",
            )
        )
        if not allowed:
            return ToolResult.failure(reason or "Symbol search denied")
        limit = min(max(int(arguments.get("limit", 50)), 1), 500)
        matches = get_index(context.workspace).find_symbols(
            str(arguments.get("query", "")),
            limit,
            path_filter=lambda path: read_allowed(context, path, self.name),
        )
        if not matches:
            return ToolResult.success("(no matching symbols)")
        lines = [
            f"{path}:{symbol.line}: {symbol.kind} {symbol.name}"
            + (f"  {symbol.signature}" if symbol.signature else "")
            for path, symbol in matches
        ]
        return ToolResult.success("\n".join(lines), data={"count": len(lines)})


class RefsTool(Tool):
    name = "refs"
    description = "Find references to a literal string using ripgrep or a workspace search."
    kind = "read"
    parameters = object_schema(
        {
            "query": string_property("Literal text or identifier to find."),
            "limit": integer_property("Maximum references to return.", 100),
            "glob": string_property("Optional file glob, for example *.py.", "*"),
        },
        ["query"],
    )

    async def run(self, context: ToolContext, **arguments: Any) -> ToolResult:
        allowed, reason = await context.authorize(
            PermissionRequest(
                tool=self.name,
                kind="read",
                path=str(context.workspace),
                preview=f"Find references to {arguments.get('query', '')}",
            )
        )
        if not allowed:
            return ToolResult.failure(reason or "Reference search denied")
        query = str(arguments.get("query", ""))
        limit = min(max(int(arguments.get("limit", 100)), 1), 1000)
        glob = str(arguments.get("glob", "*"))
        lsp_results = await find_references_lsp(
            context.workspace,
            query,
            limit=limit,
            glob=None if glob == "*" else glob,
            path_filter=lambda path: read_allowed(context, path, self.name),
        )
        if lsp_results is not None:
            return ToolResult.success(
                "\n".join(lsp_results) or "(no references)",
                data={"count": len(lsp_results), "engine": "lsp"},
            )
        results = get_index(context.workspace).find_refs(
            query, limit, glob, path_filter=lambda path: read_allowed(context, path, self.name)
        )
        return ToolResult.success(
            "\n".join(results) or "(no references)",
            data={"count": len(results), "engine": "fallback"},
        )


__all__ = ["RefsTool", "RepoMapTool", "SymbolsTool"]
