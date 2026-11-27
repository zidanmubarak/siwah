"""Tool protocol, registry, and permission bridge."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from ..config import AgentConfig
from ..core.questions import AskUser
from ..core.types import ToolSpec
from ..permissions.policy import Approval, Approver, PermissionPolicy, PermissionRequest

if TYPE_CHECKING:
    from ..core.checkpoints import CheckpointManager
    from ..core.storage import Store
    from ..core.verify import Verifier, VerifyReport


class ToolResult(BaseModel):
    ok: bool
    output: str
    error: str | None = None
    data: dict[str, Any] | None = None

    @classmethod
    def success(cls, output: str, data: dict[str, Any] | None = None) -> ToolResult:
        return cls(ok=True, output=output, data=data)

    @classmethod
    def failure(cls, error: str) -> ToolResult:
        return cls(ok=False, output=error, error=error)


EventEmitter = Callable[[str, dict[str, Any]], Awaitable[None]]
PermissionResolver = Callable[[PermissionRequest, str], Awaitable[Approval]]


class TodoItem(BaseModel):
    id: str
    content: str
    status: str = "pending"


@dataclass
class ToolContext:
    workspace: Path
    policy: PermissionPolicy
    session_id: str
    agent_config: AgentConfig
    approver: Approver | None = None
    ask_user: AskUser | None = None
    todos: list[TodoItem] = field(default_factory=list)
    read_hashes: dict[str, str] = field(default_factory=dict)
    store: Store | None = None
    tool_call_id: str | None = None
    edit_format: str = "search_replace"
    checkpointer: CheckpointManager | None = None
    verifier: Verifier | None = None
    verify_baseline: VerifyReport | None = None
    verify_report: VerifyReport | None = None
    verify_repair_attempts: int = 0
    verify_failed: bool = False
    memory_writes: int = 0
    context_tokens: int | None = None
    context_percent: float | None = None
    context_window: int | None = None
    allowance: float = 0.0
    tool_names: list[str] = field(default_factory=list)
    step: int = 0
    emit: EventEmitter | None = None
    permission_resolver: PermissionResolver | None = None
    subagent_manager: Any = None
    jobs: Any = None
    hooks: Any = None
    mcp: Any = None
    sandbox: Any = None

    async def authorize(self, request: PermissionRequest) -> tuple[bool, str | None]:
        decision = self.policy.check(request)
        if decision == "deny":
            return False, "Permission denied by policy"
        if decision == "allow":
            return True, None
        if self.approver is None:
            return False, "Permission required but no interactive approver is available"
        approval: Approval = await self.approver(request)
        self.policy.approve(request, approval)
        if approval.decision == "deny":
            return False, approval.reason or "User denied permission"
        return True, None


class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    kind: str = "read"

    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, parameters=self.parameters)

    async def run(self, context: ToolContext, **arguments: Any) -> ToolResult:
        raise NotImplementedError


class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools = {tool.name: tool for tool in tools}

    def specs(self) -> list[ToolSpec]:
        return [tool.spec() for tool in self._tools.values()]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def tools(self) -> list[Tool]:
        return list(self._tools.values())

    async def execute(
        self, name: str, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult.failure(f"Unknown tool: {name}")
        try:
            result = await tool.run(context, **arguments)
        except TypeError as exc:
            return ToolResult.failure(f"Invalid arguments for {name}: {exc}")
        except Exception as exc:  # tools must never crash the loop
            return ToolResult.failure(f"{name} failed: {exc}")
        limit = context.agent_config.tool_output_limit
        if len(result.output) > limit:
            from ..core.context import summarize_text, write_tool_output

            full = result.output
            handle = context.tool_call_id
            data = dict(result.data or {})
            if handle and context.session_id:
                output_path = write_tool_output(context.session_id, handle, full)
                data["handle"] = handle
                data["full_output"] = full
                data["output_path"] = str(output_path)
                summary = summarize_text(full, min(limit, 6_000))
                summary += f"\n... handle={handle} (call tool_output to reopen)"
            else:
                summary = full[:limit] + f"\n... truncated {len(full) - limit} characters"
            result = result.model_copy(update={"output": summary, "data": data})
        return result


def object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def string_property(description: str, default: str | None = None) -> dict[str, Any]:
    prop: dict[str, Any] = {"type": "string", "description": description}
    if default is not None:
        prop["default"] = default
    return prop


def integer_property(description: str, default: int | None = None) -> dict[str, Any]:
    prop: dict[str, Any] = {"type": "integer", "description": description}
    if default is not None:
        prop["default"] = default
    return prop


def boolean_property(description: str, default: bool = False) -> dict[str, Any]:
    return {"type": "boolean", "description": description, "default": default}


__all__ = [
    "Approval",
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "TodoItem",
    "boolean_property",
    "integer_property",
    "object_schema",
    "string_property",
]
