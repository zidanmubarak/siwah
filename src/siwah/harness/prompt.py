"""Compose the system prompt from editable harness data."""

from __future__ import annotations

import re
from pathlib import Path

from ..mcp.client import mcp_context
from ..providers.profiles import profile_path
from .bootstrap import ensure_harness
from .defaults import DEFAULT_SYSTEM_PROMPT
from .memory import agents_context, memory_context
from .skills import skill_index_text

MAX_TOOL_GUIDANCE = 8_000
MAX_MODEL_OVERLAY = 4_000


def _read(path: Path, limit: int) -> str:
    try:
        value = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(value) > limit:
        return value[:limit] + f"\n... truncated {len(value) - limit} characters"
    return value


def tool_guidance_text() -> str:
    root = ensure_harness() / "tool_guidance"
    parts: list[str] = []
    for path in sorted(root.glob("*.md"), key=lambda item: item.name.lower()):
        try:
            content = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if content:
            parts.append(content)
    text = "\n\n".join(parts)
    if len(text) > MAX_TOOL_GUIDANCE:
        text = text[:MAX_TOOL_GUIDANCE] + "\n... tool guidance truncated"
    return text


def model_overlay_text(model_ref: str | None) -> str:
    if not model_ref or "/" not in model_ref:
        return ""
    provider, _, model = model_ref.partition("/")
    if not provider or not model:
        return ""
    try:
        path = profile_path(provider, model).parent / "system_append.md"
    except (OSError, ValueError):
        return ""
    return _read(path, MAX_MODEL_OVERLAY).strip()


_PLACEHOLDER = re.compile(r"\{\{([a-z_]+)\}\}")


def _fill_all(template: str, values: dict[str, str]) -> str:
    """Substitute placeholders once; inserted values are never re-scanned."""

    used: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            return match.group(0)
        used.add(name)
        return values[name]

    prompt = _PLACEHOLDER.sub(replace, template)
    appended = [
        value.strip() for name, value in values.items() if name not in used and value.strip()
    ]
    if appended:
        prompt = prompt.rstrip() + "\n\n" + "\n\n".join(appended)
    return prompt


PLAN_MODE_PROMPT = """## Plan mode

You are in read-only plan mode. Do not modify files, run shell commands, or
claim a change was made. Investigate the repository with the read-only tools,
then produce a concrete ordered plan: goal, files to change, steps, risks, and
verification commands. The harness writes your final answer to
`.siwah/plan.md`; the user can edit it, switch to build mode, and ask you to
execute it. Use the todo tool to keep the plan's steps visible.
"""


def build_system_prompt(
    workspace: Path,
    model_ref: str | None = None,
    *,
    mode: str = "build",
    edit_format: str | None = None,
) -> str:
    """Build a deterministic prompt from harness data and current memory.

    Editing `~/.siwah/harness/prompts/system.md` changes behavior without a code
    change. Placeholders position generated sections; sections without a
    placeholder are appended so memory and skills are not silently lost.
    """

    root = ensure_harness()
    template = _read(root / "prompts" / "system.md", 32_000) or DEFAULT_SYSTEM_PROMPT
    prompt = _fill_all(
        template,
        {
            "workspace": str(workspace),
            "model": model_ref or "not configured",
            "memory": memory_context(workspace),
            "skills": skill_index_text(workspace),
            "tool_guidance": tool_guidance_text(),
            "agents_md": agents_context(workspace),
            "mcp": mcp_context(),
            "model_overlay": model_overlay_text(model_ref),
        },
    )
    if mode == "plan":
        prompt = prompt.rstrip() + "\n\n" + PLAN_MODE_PROMPT
    if edit_format:
        prompt = (
            prompt.rstrip()
            + "\n\n## Edit format\n"
            + f"Prefer the `{edit_format}` format for edit_file changes when it fits. "
            + "The tool also supports search_replace, unified_diff, and whole_file.\n"
        )
    return prompt.rstrip() + "\n"
