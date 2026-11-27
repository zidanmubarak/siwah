"""Harness tools: memory, skills, and cross-session recall."""

from __future__ import annotations

import contextlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from ..harness.memory import redact_secrets, secret_marker, write_memory
from ..harness.memory_review import review_memory
from ..harness.profile import profile_path, read_profile, update_profile
from ..harness.skill_usage import record_event, set_pinned, state_for
from ..harness.skills import (
    create_skill,
    delete_skill,
    list_skills,
    read_skill,
    skill_path,
    update_skill,
)
from ..permissions.policy import PermissionRequest
from .base import Tool, ToolContext, ToolResult, integer_property, object_schema, string_property


class MemoryWriteTool(Tool):
    name = "memory_write"
    description = (
        "Store durable user or project memory. User memory applies across projects; "
        "project memory is loaded for this workspace in later sessions. Include a "
        "source and confidence so later sessions can judge the fact."
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
            "source": {
                "type": "string",
                "enum": ["user", "agent", "system"],
                "default": "agent",
                "description": "Where this memory came from.",
            },
            "confidence": {
                "type": "string",
                "enum": ["low", "medium", "high"],
                "default": "medium",
                "description": "How strongly the evidence supports it.",
            },
        },
        ["target", "content"],
    )

    async def run(self, context: ToolContext, **arguments: Any) -> ToolResult:
        target = str(arguments.get("target", "user"))
        content = str(arguments.get("content", ""))
        mode = str(arguments.get("mode", "append"))
        source = str(arguments.get("source", "agent"))
        confidence = str(arguments.get("confidence", "medium"))
        if source not in {"user", "agent", "system"}:
            return ToolResult.failure("source must be user, agent, or system")
        if confidence not in {"low", "medium", "high"}:
            return ToolResult.failure("confidence must be low, medium, or high")
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
        if mode == "append":
            stamp = datetime.now(UTC).date().isoformat()
            content = (
                f"- {stamp} · confidence={confidence} · source={source}: "
                + content.strip().replace("\n", "\n  ")
            )
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


class ProfileReadTool(Tool):
    name = "profile_read"
    description = "Read the durable local user profile (language, stack, testing, preferences)."
    kind = "read"
    parameters = object_schema({})

    async def run(self, context: ToolContext, **arguments: Any) -> ToolResult:
        return ToolResult.success(json.dumps(read_profile(), indent=2, ensure_ascii=False))


class ProfileWriteTool(Tool):
    name = "profile_write"
    description = (
        "Update the durable local user profile. Supply only fields you have evidence for; "
        "this is not a place for secrets or one-off task details."
    )
    kind = "write"
    parameters = object_schema(
        {
            "language": string_property("Preferred response language."),
            "communication": string_property("Preferred response style and length."),
            "stack": string_property("Comma-separated languages/frameworks."),
            "testing": string_property("Comma-separated test or verification commands."),
            "risk_tolerance": string_property("How much autonomy the user prefers."),
            "preferences": string_property("Comma-separated durable preferences."),
            "notes": string_property("Other concise profile facts."),
        }
    )

    async def run(self, context: ToolContext, **arguments: Any) -> ToolResult:
        if not arguments:
            return ToolResult.failure("Supply at least one profile field")
        allowed, reason = await context.authorize(
            PermissionRequest(
                tool=self.name,
                kind="write",
                preview=f"User profile update: {', '.join(sorted(arguments))}",
            )
        )
        if not allowed:
            return ToolResult.failure(reason or "Profile write denied")
        try:
            data = update_profile(**arguments)
        except (OSError, ValueError) as exc:
            return ToolResult.failure(str(exc))
        return ToolResult.success(
            f"Updated user profile at {profile_path()}",
            data=data,
        )


class MemoryReviewTool(Tool):
    name = "memory_review"
    description = (
        "Review private user, project, and quick memory for exact duplicates or "
        "same-key conflicts. Report findings only; edit memory deliberately."
    )
    kind = "read"
    parameters = object_schema({})

    async def run(self, context: ToolContext, **arguments: Any) -> ToolResult:
        try:
            findings = review_memory(context.workspace)
        except (OSError, ValueError) as exc:
            return ToolResult.failure(str(exc))
        return ToolResult.success(
            json.dumps([item.as_dict() for item in findings], indent=2, ensure_ascii=False),
            data={"count": len(findings)},
        )


class SkillListTool(Tool):
    name = "skill_list"
    description = "Search reusable skills by name and description. Use skill_view for the body."
    parameters = object_schema(
        {
            "query": string_property("Optional search terms.", ""),
            "offset": integer_property("Number of matching skills to skip.", 0),
            "limit": integer_property("Maximum skills to show.", 20),
        }
    )

    async def run(self, context: ToolContext, **arguments: Any) -> ToolResult:
        terms = str(arguments.get("query", "")).casefold().split()
        offset = max(0, int(arguments.get("offset", 0)))
        limit = min(50, max(1, int(arguments.get("limit", 20))))
        skills = [
            skill
            for skill in list_skills(context.workspace)
            if all(term in f"{skill.name} {skill.description}".casefold() for term in terms)
        ]
        selected = skills[offset : offset + limit]
        text = (
            "\n".join(
                f"- {skill.name}: {redact_secrets(skill.description)[:240]}" for skill in selected
            )
            or "(no matching skills)"
        )
        if offset + limit < len(skills):
            text += f"\nMore matches: use offset={offset + limit}."
        return ToolResult.success(text)


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
                "enum": [
                    "create",
                    "update",
                    "edit",
                    "patch",
                    "delete",
                    "list",
                    "pin",
                    "unpin",
                ],
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
                record_event(skill.name, "create", created_by="agent")
                return ToolResult.success(f"Created skill {skill.name} at {skill.path}")
            if action in {"update", "edit"}:
                skill = update_skill(
                    name,
                    description=arguments.get("description"),
                    content=arguments.get("content"),
                    workspace=context.workspace,
                )
                record_event(skill.name, "patch")
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
                record_event(skill.name, "patch")
                return ToolResult.success(f"Patched skill {skill.name} at {skill.path}")
            if action in {"pin", "unpin"}:
                if not skill_path(name, context.workspace).is_file():
                    return ToolResult.failure(f"Skill not found: {name}")
                set_pinned(name, action == "pin")
                record_event(name, action)
                return ToolResult.success(
                    f"{'Pinned' if action == 'pin' else 'Unpinned'} skill {name}"
                )
            if action == "delete":
                deleted = delete_skill(name, context.workspace)
                if deleted:
                    record_event(name, "delete")
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
        with contextlib.suppress(OSError, ValueError):
            record_event(skill.name, "view")
        return ToolResult.success(
            f"# {skill.name}\n{skill.description}\n\n{skill.content}",
            data={"name": skill.name, "path": str(skill.path), "state": state_for(skill.name)},
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
        "message IDs, and short snippets. Use message_id to read nearby messages."
    )
    kind = "read"
    parameters = object_schema(
        {
            "query": string_property("Distinctive words to search for in this project."),
            "message_id": string_property("Open nearby messages around a search result."),
            "radius": integer_property("Number of adjacent message positions on either side.", 2),
            "limit": {
                "type": "integer",
                "default": 10,
                "minimum": 1,
                "maximum": 50,
            },
            "include_current": {
                "type": "boolean",
                "default": False,
                "description": "Include recent messages from the current session as well as compacted messages.",
            },
        },
    )

    async def run(self, context: ToolContext, **arguments: Any) -> ToolResult:
        if context.store is None:
            return ToolResult.failure("Session storage is unavailable")
        current = context.store.get_session(context.session_id)
        cwd = current.cwd if current else str(context.workspace)
        cutoff = (current.metadata or {}).get("context_compacted_through", 0) if current else 0
        through = cutoff if isinstance(cutoff, int) else 0
        include_current = bool(arguments.get("include_current", False))
        message_id = arguments.get("message_id")
        if message_id:
            rows = context.store.recall_window(
                str(message_id),
                cwd=cwd,
                radius=min(5, max(0, int(arguments.get("radius", 2)))),
                current_id=context.session_id,
                through=None if include_current else through,
            )
            contents = [
                f"[{row['message_id']}] {row['role']}: "
                + redact_secrets(str(row["content"] or ""))[:1500]
                for row in rows
            ]
            return ToolResult.success(
                "\n\n".join(contents) or "(no matching messages in this project)"
            )
        try:
            query = _fts_query(str(arguments.get("query", "")))
        except ValueError as exc:
            return ToolResult.failure(str(exc))
        limit = min(max(int(arguments.get("limit", 10)), 1), 50)
        rows = context.store.recall_messages(
            query,
            limit,
            exclude_session_id=None if include_current else context.session_id,
            compacted_through=through,
            cwd=cwd,
        )
        if not rows:
            return ToolResult.success("(no matching sessions)")
        lines: list[str] = []
        for row in rows:
            snippet = redact_secrets(str(row.get("content") or "")).replace("\n", " ")
            snippet = snippet[:400]
            title = row.get("title") or row.get("session_id")
            lines.append(
                f"[{row.get('session_id')}] {title} (message_id: {row.get('message_id')})\n"
                f"  {row.get('cwd')} · {row.get('updated_at')}\n"
                f"  {snippet}"
            )
        return ToolResult.success("\n\n".join(lines), data={"count": len(rows)})
