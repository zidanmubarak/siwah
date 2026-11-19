"""Reopen a tool result that was pruned or offloaded."""

from __future__ import annotations

from typing import Any

from ..core.context import read_tool_output, tool_output_path
from ..harness.memory import redact_secrets
from ..permissions.policy import PermissionRequest
from .base import Tool, ToolContext, ToolResult, integer_property, object_schema, string_property


class ToolOutputTool(Tool):
    name = "tool_output"
    description = "Reopen a full tool result by its handle when an older result was pruned."
    kind = "read"
    parameters = object_schema(
        {
            "handle": string_property("The tool call id shown in the pruned result or tool card."),
            "max_chars": integer_property("Maximum characters to return.", 20000),
        },
        ["handle"],
    )

    async def run(self, context: ToolContext, **arguments: Any) -> ToolResult:
        handle = str(arguments.get("handle", "")).strip()
        if not handle:
            return ToolResult.failure("handle is required")
        try:
            tool_output_path(context.session_id, handle)
        except ValueError:
            return ToolResult.failure("Invalid tool output handle")
        allowed, reason = await context.authorize(
            PermissionRequest(
                tool=self.name,
                kind="read",
                preview=f"Reopen tool output {handle}",
            )
        )
        if not allowed:
            return ToolResult.failure(reason or "Tool output denied")
        limit = min(max(int(arguments.get("max_chars", 20_000)), 1), 200_000)
        output: str | None = None
        if context.store is not None:
            record = context.store.get_tool_call(handle)
            if record and record["session_id"] == context.session_id:
                result = record.get("result") or {}
                data = result.get("data") if isinstance(result, dict) else {}
                if isinstance(data, dict) and isinstance(data.get("full_output"), str):
                    output = data["full_output"]
                elif isinstance(result, dict) and isinstance(result.get("output"), str):
                    output = result["output"]
        if output is None:
            output = read_tool_output(context.session_id, handle)
        if output is None:
            return ToolResult.failure(f"No stored output for handle {handle}")
        output = redact_secrets(output)
        if len(output) > limit:
            output = output[:limit] + f"\n... truncated {len(output) - limit} characters"
        return ToolResult.success(output, data={"handle": handle})


__all__ = ["ToolOutputTool"]
