"""Subagent specifications and isolated child runs."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import CheckpointConfig, Config, VerifyConfig
from ..paths import harness_dir
from ..permissions.policy import Approval, PermissionRequest
from ..plugins import plugin_subagent_roots
from ..providers.registry import RoleRouter
from ..tools.base import Tool, ToolRegistry
from .storage import Session, Store

PermissionResolver = Callable[[PermissionRequest, str], Awaitable[Approval]]
EventSink = Callable[[str, dict[str, Any]], Awaitable[None]]
AskUser = Callable[[str], Awaitable[str]]

READ_ONLY_TOOLS = {
    "read_file",
    "glob",
    "grep",
    "repo_map",
    "symbols",
    "refs",
    "tool_output",
    "skill_view",
    "recall",
    "todo",
}
DEFAULT_GENERAL_TOOLS = {
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "grep",
    "bash",
    "todo",
    "repo_map",
    "symbols",
    "refs",
    "tool_output",
}


@dataclass(frozen=True)
class SubagentSpec:
    name: str
    description: str
    role: str = "main"
    model: str | None = None
    tools: tuple[str, ...] = field(default_factory=tuple)
    read_only: bool = False
    body: str = ""

    def allowed_tools(self, available: set[str]) -> set[str]:
        if self.tools:
            selected = set(self.tools)
        else:
            selected = READ_ONLY_TOOLS if self.read_only else DEFAULT_GENERAL_TOOLS
        selected &= available
        selected.discard("task")
        if self.read_only:
            selected &= READ_ONLY_TOOLS
        return selected


DEFAULT_SUBAGENTS: tuple[SubagentSpec, ...] = (
    SubagentSpec(
        name="explore",
        description="Read-only investigation of files, symbols, and references.",
        role="plan",
        read_only=True,
        body="Explore the workspace and return a concise evidence-based answer.",
    ),
    SubagentSpec(
        name="general",
        description="General implementation subagent with a bounded tool set.",
        role="main",
        body="Complete the delegated task and report the changes and verification.",
    ),
    SubagentSpec(
        name="review",
        description="Read-only review of changes, risks, and missing tests.",
        role="main",
        read_only=True,
        body="Review the requested area and return findings ordered by severity.",
    ),
)


def load_subagents(
    root: Path | str | None = None, *, include_defaults: bool = True
) -> dict[str, SubagentSpec]:
    specs = {spec.name: spec for spec in DEFAULT_SUBAGENTS} if include_defaults else {}
    bases = (
        [Path(root)]
        if root is not None
        else [
            harness_dir() / "subagents",
            *plugin_subagent_roots(),
        ]
    )
    for base in bases:
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            metadata, body = _parse_frontmatter(text)
            name = metadata.get("name", path.stem).strip()
            if not name:
                continue
            tools = tuple(
                item.strip() for item in metadata.get("tools", "").split(",") if item.strip()
            )
            read_only = metadata.get("read_only", "false").strip().lower() in {
                "1",
                "true",
                "yes",
            }
            specs[name] = SubagentSpec(
                name=name,
                description=metadata.get("description", "").strip() or name,
                role=metadata.get("role", "main").strip() or "main",
                model=metadata.get("model", "").strip() or None,
                tools=tools,
                read_only=read_only,
                body=body.strip(),
            )
    return specs


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        return {}, text
    metadata: dict[str, str] = {}
    for line in lines[1:end]:
        key, separator, raw = line.partition(":")
        if not separator:
            continue
        value = raw.strip()
        if value.startswith('"') and value.endswith('"'):
            with contextlib.suppress(json.JSONDecodeError):
                value = json.loads(value)
        metadata[key.strip()] = value
    return metadata, "\n".join(lines[end + 1 :]).lstrip("\n")


@dataclass(frozen=True)
class SubagentResult:
    name: str
    text: str
    session_id: str
    usage: Any
    error: str | None = None


class SubagentManager:
    def __init__(
        self,
        *,
        config: Config,
        store: Store,
        router: RoleRouter,
        tools: list[Tool],
        parent_session: Session,
        emit: EventSink,
        permission_resolver: PermissionResolver | None = None,
        ask_user: AskUser | None = None,
        root: Path | str | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.router = router
        self.tools = tools
        self.parent_session = parent_session
        self.emit = emit
        self.permission_resolver = permission_resolver
        self.ask_user = ask_user
        self.root = root
        self.specs: dict[str, SubagentSpec] = {}
        self.reload_specs()

    def reload_specs(self) -> None:
        """Reload global and project specs so a file created this run is usable."""

        self.specs = load_subagents(self.root)
        project_root = Path(self.parent_session.cwd) / ".siwah" / "subagents"
        if project_root.is_dir():
            self.specs.update(load_subagents(project_root, include_defaults=False))

    async def run_many(self, tasks: list[tuple[str, str, str]]) -> list[SubagentResult]:
        self.reload_specs()
        specs = [self.specs.get(name) for name, _prompt, _description in tasks]
        missing = [
            name
            for name, spec in zip((item[0] for item in tasks), specs, strict=True)
            if spec is None
        ]
        if missing:
            raise ValueError(f"Unknown subagent: {missing[0]}")
        if any(spec is not None and not spec.read_only for spec in specs):
            raise ValueError("parallel task batches are limited to read-only subagents")
        return list(
            await asyncio.gather(
                *(
                    self.run(name, prompt, description=description, _spec=spec)
                    for (name, prompt, description), spec in zip(tasks, specs, strict=True)
                )
            )
        )

    async def run(
        self,
        name: str,
        prompt: str,
        *,
        description: str = "",
        _spec: SubagentSpec | None = None,
    ) -> SubagentResult:
        if _spec is None:
            self.reload_specs()
        spec = _spec or self.specs.get(name)
        if spec is None:
            raise ValueError(f"Unknown subagent: {name}")
        from .loop import AgentLoop

        available = {tool.name for tool in self.tools}
        allowed = spec.allowed_tools(available)
        tools = ToolRegistry([tool for tool in self.tools if tool.name in allowed])
        child_config = self.config.model_copy(
            update={
                "checkpoints": CheckpointConfig(enabled=False),
                "verify": VerifyConfig(auto=False),
                "subagents": self.config.subagents.model_copy(update={"enabled": False}),
            }
        )
        model = spec.model or self.parent_session.model or self.config.roles.main
        child = self.store.create_session(
            cwd=self.parent_session.cwd,
            model=model,
            title=(description or f"subagent: {spec.name}")[:80],
        )
        self.store.update_session(
            child.id,
            metadata={
                "parent_session_id": self.parent_session.id,
                "subagent": spec.name,
                "depth": 1,
            },
        )
        body = spec.body or f"You are the {spec.name} subagent."
        child_prompt = f"{body}\n\nTask:\n{prompt}"

        async def child_emit(event: str, data: dict[str, Any]) -> None:
            if event in {"permission.request", "permission.resolved"}:
                # The UI resolves permissions by request_id at the top level; a
                # nested envelope would leave the future waiting forever.
                payload = dict(data)
                payload.setdefault("session_id", child.id)
                payload["subagent"] = spec.name
                await self.emit(event, payload)
                return
            await self.emit(
                "subagent.event",
                {"subagent": spec.name, "session_id": child.id, "event": event, "data": data},
            )

        loop = AgentLoop(config=child_config, store=self.store, router=self.router, tools=tools)
        await self.emit(
            "subagent.start",
            {"subagent": spec.name, "session_id": child.id, "description": description},
        )
        try:
            result = await loop.run(
                child,
                child_prompt,
                emit=child_emit,
                permission_resolver=self.permission_resolver,
                ask_user=self.ask_user,
                model_override=model,
                role=spec.role,
                max_steps=self.config.subagents.max_steps,
                max_allowance=self.config.subagents.max_allowance,
            )
        except Exception as exc:
            await self.emit(
                "subagent.end",
                {"subagent": spec.name, "session_id": child.id, "error": str(exc)},
            )
            return SubagentResult(spec.name, "", child.id, None, str(exc))
        finally:
            await loop.close_jobs()
        error = result.error
        await self.emit(
            "subagent.end",
            {
                "subagent": spec.name,
                "session_id": child.id,
                "text": result.text[:2_000],
                "allowance": result.usage.allowance,
                "error": error,
            },
        )
        return SubagentResult(spec.name, result.text, child.id, result.usage, error)


__all__ = [
    "DEFAULT_SUBAGENTS",
    "DEFAULT_GENERAL_TOOLS",
    "READ_ONLY_TOOLS",
    "SubagentManager",
    "SubagentResult",
    "SubagentSpec",
    "load_subagents",
]
