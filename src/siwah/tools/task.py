"""The `task` tool: launch an isolated subagent."""

from __future__ import annotations

from typing import Any

from ..permissions.policy import PermissionRequest
from .base import Tool, ToolContext, ToolResult, object_schema, string_property


class TaskTool(Tool):
    name = "task"
    description = (
        "Launch isolated subagents. Use explore for read-only investigation, "
        "review for read-only review, or general for implementation. Custom names "
        "are loaded from global and project .siwah/subagents; a file created in "
        "this run is usable immediately. Read-only tasks can be batched with "
        "`tasks` and run in parallel. Subagents cannot launch further subagents."
    )
    kind = "read"
    parameters = object_schema(
        {
            "subagent_type": string_property("explore, review, general, or a custom name."),
            "prompt": string_property("Task instructions for the subagent."),
            "description": string_property("Short label shown in the UI."),
            "tasks": {
                "type": "array",
                "description": "Parallel read-only tasks; do not combine with prompt.",
                "items": {
                    "type": "object",
                    "properties": {
                        "subagent_type": {"type": "string"},
                        "prompt": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["subagent_type", "prompt"],
                },
            },
        }
    )

    async def run(self, context: ToolContext, **arguments: Any) -> ToolResult:
        manager = context.subagent_manager
        if manager is None:
            return ToolResult.failure("Subagents are disabled for this session")
        tasks = arguments.get("tasks")
        if tasks is not None:
            if not isinstance(tasks, list) or not tasks:
                return ToolResult.failure("tasks must be a non-empty array")
            if any(
                not isinstance(item, dict) or "subagent_type" not in item or "prompt" not in item
                for item in tasks
            ):
                return ToolResult.failure("Each task requires subagent_type and prompt")
            allowed, reason = await context.authorize(
                PermissionRequest(
                    tool=self.name,
                    kind="read",
                    preview=f"Launch {len(tasks)} read-only subagents",
                )
            )
            if not allowed:
                return ToolResult.failure(reason or "Task denied")
            try:
                results = await manager.run_many(
                    [
                        (
                            str(item["subagent_type"]),
                            str(item["prompt"]),
                            str(item.get("description", "")),
                        )
                        for item in tasks
                        if isinstance(item, dict)
                    ]
                )
            except (KeyError, ValueError) as exc:
                return ToolResult.failure(f"Invalid task batch: {exc}")
            output = "\n\n".join(f"[{result.name}] {result.text}" for result in results)
            errors = [result.error for result in results if result.error]
            return ToolResult(
                ok=not errors,
                output=output,
                error="; ".join(errors) if errors else None,
                data={
                    "subagents": [result.name for result in results],
                    "sessions": [result.session_id for result in results],
                    "allowance": sum(
                        result.usage.allowance if result.usage else 0.0 for result in results
                    ),
                },
            )
        if "subagent_type" not in arguments or "prompt" not in arguments:
            return ToolResult.failure("subagent_type and prompt are required")
        name = str(arguments["subagent_type"])
        prompt = str(arguments["prompt"])
        description = str(arguments.get("description", ""))
        reload_specs = getattr(manager, "reload_specs", None)
        if callable(reload_specs):
            reload_specs()
        specs = getattr(manager, "specs", None)
        spec = specs.get(name) if isinstance(specs, dict) else None
        if isinstance(specs, dict) and spec is None:
            return ToolResult.failure(f"Unknown subagent: {name}")
        read_only = spec.read_only if spec is not None else name in {"explore", "review"}
        allowed, reason = await context.authorize(
            PermissionRequest(
                tool=self.name,
                kind="read" if read_only else "write",
                preview=f"Launch {'read-only' if read_only else 'writable'} subagent {name}: "
                + (description or prompt)[:500],
            )
        )
        if not allowed:
            return ToolResult.failure(reason or "Task denied")
        try:
            options = {"_spec": spec} if spec is not None else {}
            result = await manager.run(name, prompt, description=description, **options)
        except ValueError as exc:
            return ToolResult.failure(str(exc))
        output = result.text
        if result.error:
            output = f"{output}\n\n[subagent warning] {result.error}"
        return ToolResult(
            ok=not result.error,
            output=output,
            error=result.error,
            data={
                "subagent": result.name,
                "session_id": result.session_id,
                "allowance": result.usage.allowance if result.usage else 0.0,
                "error": result.error,
                # Conservative mutation signal, including partial/error runs.
                # The parent owns verification and its rewind checkpoint.
                "writable": not read_only,
            },
        )


__all__ = ["TaskTool"]
