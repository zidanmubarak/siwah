"""Stable system prompt for the agent loop.

The body lives in `~/.siwah/harness/prompts/system.md`; this module keeps the
compatibility import used by the loop and tests.
"""

from __future__ import annotations

from pathlib import Path

from ..harness.prompt import build_system_prompt


def system_prompt(
    workspace: Path,
    model_ref: str | None = None,
    *,
    mode: str = "build",
    edit_format: str | None = None,
) -> str:
    return build_system_prompt(workspace, model_ref, mode=mode, edit_format=edit_format)
