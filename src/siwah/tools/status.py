"""Read-only runtime status for the agent itself."""

from __future__ import annotations

import json
from typing import Any

from ..adapt.state import load_state
from ..harness.memory import read_memory
from ..harness.skills import list_skills
from ..version import __version__
from .base import Tool, ToolContext, ToolResult, object_schema


def _job_status(context: ToolContext) -> list[dict[str, Any]]:
    manager = context.jobs
    if manager is None:
        return []
    rows: list[dict[str, Any]] = []
    for job in manager.list()[:12]:
        rows.append(
            {
                "id": job.id,
                "status": job.status,
                "command": job.command[:240],
                "returncode": job.returncode,
                "lines_read": job.lines_read,
            }
        )
    return rows


class SelfStatusTool(Tool):
    name = "self_status"
    description = (
        "Inspect your own runtime status before guessing: available tools, context and "
        "allowance usage, permission mode, active background work, memory/skill counts, "
        "verification result, and evolution state."
    )
    kind = "read"
    parameters = object_schema({})

    async def run(self, context: ToolContext, **arguments: Any) -> ToolResult:
        session = (
            context.store.get_session(context.session_id) if context.store is not None else None
        )
        skills = list_skills(context.workspace)
        memory = read_memory(context.workspace)
        evolution = load_state()
        verification = context.verify_report
        subagent_manager = context.subagent_manager
        background_subagents = (
            subagent_manager.background_count()
            if subagent_manager is not None
            and callable(getattr(subagent_manager, "background_count", None))
            else 0
        )
        payload: dict[str, Any] = {
            "version": __version__,
            "session": {
                "id": context.session_id,
                "mode": session.mode if session else None,
                "model": session.model if session else None,
                "cwd": str(context.workspace),
                "status": session.status if session else None,
            },
            "context": {
                "tokens": context.context_tokens,
                "percent": context.context_percent,
                "window": context.context_window,
            },
            "allowance": context.allowance,
            "permission_mode": context.policy.config.mode,
            "tools": context.tool_names,
            "skills": {
                "count": len(skills),
                "names": [skill.name for skill in skills[:30]],
            },
            "memory": {
                "user_chars": len(memory.user.content),
                "project_chars": len(memory.project.content),
                "quick_chars": len(memory.quick.content),
            },
            "jobs": _job_status(context),
            "background_subagents": background_subagents,
            "verification": (
                {"status": verification.status, "summary": verification.summary[:1200]}
                if verification is not None
                else None
            ),
            "evolution": {
                "tag": evolution.tag,
                "status": evolution.status,
                "canary_tag": evolution.canary_tag,
            },
        }
        return ToolResult.success(
            json.dumps(payload, indent=2, ensure_ascii=False),
            data=payload,
        )


__all__ = ["SelfStatusTool"]
