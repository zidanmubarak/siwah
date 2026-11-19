"""Harness tools: memory, skills, and cross-session recall."""

from __future__ import annotations

import re
from typing import Any

from ..harness.memory import redact_secrets, secret_marker, write_memory
from ..harness.skills import (
    create_skill,
    delete_skill,
    list_skills,
    read_skill,
    skill_path,
    update_skill,
)
from ..permissions.policy import PermissionRequest
from .base import Tool, ToolContext, ToolResult, object_schema, string_property


class MemoryWriteTool(Tool):
    name = "memory_write"
    description = (
        "Store durable user or project memory. User memory applies across projects; "
        "project memory is loaded for this workspace in later sessions."
    )
    kind = "write"
    parameters = object_schema(
        {
            "target": {
                "type": "string",
                "enum": ["user", "project"],
                "description": "user (global preferences) or project (workspace facts).",
            },
            "content": string_property("Memory text to store."),
            "mode": {
                "type": "string",
                "enum": ["append", "replace"],
                "default": "append",
                "description": "Append a block or replace the whole file.",
            },
        },
        ["target", "content"],
    )

    async def run(self, context: ToolContext, **arguments: Any) -> ToolResult:
        target = str(arguments.get("target", "user"))
        content = str(arguments.get("content", ""))
        mode = str(arguments.get("mode", "append"))
        marker = secret_marker(content)
        if marker:
            return ToolResult.failure(f"refusing to store a {marker}")
        allowed, reason = await context.authorize(
            PermissionRequest(
                tool=self.name,
                kind="write",
                preview=f"Memory ({target}, {mode}): {content[:160]}",
            )
        )
        if not allowed:
            return ToolResult.failure(reason or "Memory write denied")
        try:
            path, changed = write_memory(
                context.workspace, target=target, content=content, mode=mode
            )
        except (OSError, ValueError) as exc:
            return ToolResult.failure(str(exc))
        if not changed:
            return ToolResult.success(f"Memory already contains this content: {path}")
        return ToolResult.success(
            f"Wrote {target} memory to {path}. It is loaded in the next session.",
            data={"path": str(path), "target": target},
        )


class SkillManageTool(Tool):
    name = "skill_manage"
    description = (
        "Create, update, list, or delete a reusable SKILL.md under the harness. "
        "Use skill_view to load a skill body."
    )
    kind = "write"
    parameters = object_schema(
        {
            "action": {
                "type": "string",
                "enum": ["create", "update", "edit", "patch", "delete", "list"],
            },
            "name": string_property("Skill name (lowercase, digits, hyphens)."),
            "description": string_property("Short description shown in the skill index."),
            "content": string_property("Full Markdown skill body."),
            "old_string": string_property("Exact body text to replace for patch."),
            "new_string": string_property("Replacement text for patch."),
            "overwrite": {
                "type": "boolean",
                "default": False,
                "description": "Allow create to replace an existing skill.",
            },
        },
        ["action"],
    )

    async def run(self, context: ToolContext, **arguments: Any) -> ToolResult:
        action = str(arguments.get("action", "list"))
        name = str(arguments.get("name") or "")
        if action == "list":
            skills = list_skills(context.workspace)
            if not skills:
                return ToolResult.success("(no skills)")
            return ToolResult.success(
                "\n".join(f"- {skill.name}: {skill.description}" for skill in skills)
            )
        try:
            path = skill_path(name, context.workspace)
        except ValueError as exc:
            return ToolResult.failure(str(exc))
        allowed, reason = await context.authorize(
            PermissionRequest(
                tool=self.name,
                kind="write",
                path=str(path),
                preview=f"Skill {action}: {name}",
            )
        )
        if not allowed:
            return ToolResult.failure(reason or "Skill change denied")
        try:
            if action == "create":
                skill = create_skill(
                    name,
                    str(arguments.get("description", "")),
                    str(arguments.get("content", "")),
                    overwrite=bool(arguments.get("overwrite", False)),
                )
                return ToolResult.success(f"Created skill {skill.name} at {skill.path}")
            if action in {"update", "edit"}:
                skill = update_skill(
                    name,
                    description=arguments.get("description"),
                    content=arguments.get("content"),
                    workspace=context.workspace,
                )
                return ToolResult.success(f"Updated skill {skill.name} at {skill.path}")
            if action == "patch":
                current = read_skill(name, context.workspace)
                old = str(arguments.get("old_string", ""))
                new = str(arguments.get("new_string", ""))
                if not old:
                    return ToolResult.failure("old_string is required for patch")
                count = current.content.count(old)
                if count == 0:
                    return ToolResult.failure("old_string was not found in the skill body")
                if count > 1:
                    return ToolResult.failure(
                        f"old_string occurs {count} times; make the patch unique"
                    )
                skill = update_skill(
                    name,
                    content=current.content.replace(old, new, 1),
                    workspace=context.workspace,
                )
                return ToolResult.success(f"Patched skill {skill.name} at {skill.path}")
            if action == "delete":
                deleted = delete_skill(name, context.workspace)
                return ToolResult.success(
                    f"Deleted skill {name}" if deleted else f"Skill not found: {name}"
                )
        except (OSError, ValueError) as exc:
            return ToolResult.failure(str(exc))
        return ToolResult.failure(f"Unknown skill action: {action}")


class SkillViewTool(Tool):
    name = "skill_view"
    description = "Load the full SKILL.md body for a skill by name."
    kind = "read"
    parameters = object_schema(
        {"name": string_property("Skill name from the skill index.")},
        ["name"],
    )

    async def run(self, context: ToolContext, **arguments: Any) -> ToolResult:
        name = str(arguments.get("name", ""))
        try:
            path = skill_path(name, context.workspace)
        except ValueError as exc:
            return ToolResult.failure(str(exc))
        allowed, reason = await context.authorize(
            PermissionRequest(
                tool=self.name,
                kind="read",
                path=str(path),
                preview=f"Read skill {name}",
            )
        )
        if not allowed:
            return ToolResult.failure(reason or "Skill read denied")
        try:
            skill = read_skill(name, context.workspace)
        except (FileNotFoundError, OSError) as exc:
            return ToolResult.failure(str(exc))
        return ToolResult.success(
            f"# {skill.name}\n{skill.description}\n\n{skill.content}",
            data={"name": skill.name, "path": str(skill.path)},
        )


def _fts_query(value: str) -> str:
    tokens = re.findall(r"\w+", value, flags=re.UNICODE)
    if not tokens:
        raise ValueError("query must contain at least one word")
    return " ".join(f'"{token}"' for token in tokens[:12])


class RecallTool(Tool):
    name = "recall"
    description = (
        "Search earlier session messages by keywords. Returns session ids, titles, "
        "workspace paths, and short snippets."
    )
    kind = "read"
    parameters = object_schema(
        {
            "query": string_property("Distinctive words to search for."),
            "limit": {
                "type": "integer",
                "default": 10,
                "minimum": 1,
                "maximum": 50,
            },
            "include_current": {
                "type": "boolean",
                "default": False,
                "description": "Include the current session in results.",
            },
        },
        ["query"],
    )

    async def run(self, context: ToolContext, **arguments: Any) -> ToolResult:
        if context.store is None:
            return ToolResult.failure("Session storage is unavailable")
        try:
            query = _fts_query(str(arguments.get("query", "")))
        except ValueError as exc:
            return ToolResult.failure(str(exc))
        limit = min(max(int(arguments.get("limit", 10)), 1), 50)
        rows = context.store.recall_messages(
            query,
            limit,
            exclude_session_id=None if arguments.get("include_current") else context.session_id,
        )
        if not rows:
            return ToolResult.success("(no matching sessions)")
        lines: list[str] = []
        for row in rows:
            snippet = redact_secrets(str(row.get("content") or "")).replace("\n", " ")
            snippet = snippet[:400]
            title = row.get("title") or row.get("session_id")
            lines.append(
                f"[{row.get('session_id')}] {title}\n"
                f"  {row.get('cwd')} · {row.get('updated_at')}\n"
                f"  {snippet}"
            )
        return ToolResult.success("\n\n".join(lines), data={"count": len(rows)})
