"""Run the project verifier on demand."""

from __future__ import annotations

from typing import Any

from .base import Tool, ToolContext, ToolResult, object_schema


class VerifyTool(Tool):
    name = "verify"
    description = (
        "Run the project's configured tests, lint, and type checks now. Use it before "
        "claiming a change is complete and report the result honestly."
    )
    kind = "bash"
    parameters = object_schema({})

    async def run(self, context: ToolContext, **arguments: Any) -> ToolResult:
        if context.verifier is None:
            return ToolResult.failure("Verifier is unavailable for this session")
        report = await context.verifier.run()
        context.verify_report = report
        return ToolResult(
            ok=report.status in {"passed", "flaky"},
            output=report.summary,
            error=None if report.status in {"passed", "flaky"} else "verification failed",
            data={"status": report.status, "commands": [item.name for item in report.commands]},
        )


__all__ = ["VerifyTool"]
