"""Local hook commands with a JSON stdin/stdout contract."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .jobs import safe_environment
from .paths import harness_dir


@dataclass(frozen=True)
class HookSpec:
    name: str
    event: str
    command: tuple[str, ...]
    matcher: str | None = None
    timeout_s: float = 15.0
    cwd: str | None = None


@dataclass(frozen=True)
class HookResult:
    allow: bool | None = None
    reason: str | None = None
    context: str = ""
    errors: tuple[str, ...] = ()


def load_hooks(root: Path | str | None = None) -> list[HookSpec]:
    base = Path(root) if root is not None else harness_dir() / "hooks"
    if not base.is_dir():
        return []
    hooks: list[HookSpec] = []
    for path in sorted(base.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        command = payload.get("command")
        if isinstance(command, str):
            command = [command]
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(item, str) for item in command)
        ):
            continue
        event = str(payload.get("event", "")).strip()
        if not event:
            continue
        hooks.append(
            HookSpec(
                name=str(payload.get("name", path.stem)),
                event=event,
                command=tuple(command),
                matcher=str(payload["matcher"]) if payload.get("matcher") else None,
                timeout_s=float(payload.get("timeout_s", 15.0)),
                cwd=str(path.parent),
            )
        )
    return hooks


class HookManager:
    def __init__(self, hooks: list[HookSpec]) -> None:
        self.hooks = hooks

    @classmethod
    def load(cls, root: Path | str | None = None) -> HookManager:
        return cls(load_hooks(root))

    async def run(self, event: str, payload: dict[str, Any]) -> HookResult:
        allow: bool | None = None
        reasons: list[str] = []
        contexts: list[str] = []
        errors: list[str] = []
        tool = str(payload.get("tool", ""))
        for hook in self.hooks:
            if hook.event != event:
                continue
            if hook.matcher and tool and not fnmatch.fnmatch(tool, hook.matcher):
                continue
            body = json.dumps({"event": event, **payload}, ensure_ascii=False)
            try:
                completed = await asyncio.to_thread(
                    subprocess.run,
                    list(hook.command),
                    input=body,
                    cwd=hook.cwd,
                    env=safe_environment(),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=hook.timeout_s,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                errors.append(f"{hook.name}: {exc}")
                if event == "PreToolUse":
                    allow = False
                    reasons.append(f"{hook.name}: hook failed")
                continue
            if completed.returncode != 0:
                errors.append(f"{hook.name}: exited {completed.returncode}")
                if event == "PreToolUse":
                    allow = False
                    reasons.append(f"{hook.name}: hook failed")
                continue
            raw = (completed.stdout or "").strip()
            if not raw:
                continue
            try:
                output = json.loads(raw)
            except json.JSONDecodeError:
                errors.append(f"{hook.name}: invalid JSON output")
                if event == "PreToolUse":
                    allow = False
                    reasons.append(f"{hook.name}: invalid JSON")
                continue
            if not isinstance(output, dict):
                continue
            if output.get("allow") is False:
                allow = False
                reasons.append(str(output.get("reason", f"{hook.name}: denied")))
            elif output.get("allow") is True and allow is None:
                allow = True
            if output.get("context"):
                contexts.append(str(output["context"]))
        return HookResult(
            allow=allow,
            reason="; ".join(reasons) or None,
            context="\n".join(contexts),
            errors=tuple(errors),
        )


__all__ = ["HookManager", "HookResult", "HookSpec", "load_hooks"]
