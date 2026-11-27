"""Tools for writing and searching private experience lessons."""

from __future__ import annotations

from typing import Any

from ..harness.lessons import format_lessons, search_lessons, write_lesson
from ..permissions.policy import PermissionRequest
from .base import Tool, ToolContext, ToolResult, integer_property, object_schema, string_property


class LessonWriteTool(Tool):
    name = "lesson_write"
    description = (
        "Store one concise, reusable lesson from a completed task. Use a recognizable cue, "
        "the transferable rule, evidence, and a confidence level. Lessons are private memory; "
        "promote a mature lesson to a skill when it becomes a procedure."
    )
    kind = "write"
    parameters = object_schema(
        {
            "target": {
                "type": "string",
                "enum": ["user", "project"],
                "description": "User lessons apply across projects; project lessons stay local.",
            },
            "cue": string_property("When should this lesson be recalled?"),
            "lesson": string_property("The reusable rule or insight."),
            "evidence": string_property("Command, test, file, or observation that supports it."),
            "confidence": {
                "type": "string",
                "enum": ["low", "medium", "high"],
                "default": "medium",
            },
        },
        ["target", "cue", "lesson"],
    )

    async def run(self, context: ToolContext, **arguments: Any) -> ToolResult:
        target = str(arguments.get("target", "user"))
        cue = str(arguments.get("cue", ""))
        lesson = str(arguments.get("lesson", ""))
        evidence = str(arguments.get("evidence", ""))
        confidence = str(arguments.get("confidence", "medium"))
        allowed, reason = await context.authorize(
            PermissionRequest(
                tool=self.name,
                kind="write",
                path=str(context.workspace),
                preview=f"Lesson ({target}): {cue[:160]}",
            )
        )
        if not allowed:
            return ToolResult.failure(reason or "Lesson write denied")
        try:
            path, changed = write_lesson(
                context.workspace,
                target=target,
                cue=cue,
                lesson=lesson,
                evidence=evidence,
                confidence=confidence,
            )
        except (OSError, ValueError) as exc:
            return ToolResult.failure(str(exc))
        if not changed:
            return ToolResult.success(f"Lesson already recorded: {path}")
        return ToolResult.success(
            f"Wrote {target} lesson to {path}. It is loaded in later sessions.",
            data={"path": str(path), "target": target},
        )


class LessonSearchTool(Tool):
    name = "lesson_search"
    description = "Search private experience lessons before re-solving a similar task."
    kind = "read"
    parameters = object_schema(
        {
            "query": string_property("Distinctive words from the task or situation."),
            "limit": integer_property("Maximum lessons to return.", 8),
        },
        ["query"],
    )

    async def run(self, context: ToolContext, **arguments: Any) -> ToolResult:
        query = str(arguments.get("query", ""))
        limit = min(50, max(1, int(arguments.get("limit", 8))))
        try:
            lessons = search_lessons(context.workspace, query, limit=limit)
        except (OSError, ValueError) as exc:
            return ToolResult.failure(str(exc))
        return ToolResult.success(format_lessons(lessons), data={"count": len(lessons)})


__all__ = ["LessonSearchTool", "LessonWriteTool"]
