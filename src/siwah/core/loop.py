"""The agent loop: provider streaming, tools, permissions, persistence."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..config import Config, ConfigError
from ..core.checkpoints import CheckpointManager
from ..core.context import (
    ContextManager,
    compaction_prompt,
    fallback_summary,
)
from ..core.prompt import system_prompt
from ..core.storage import Session, Store, UsageTotals
from ..core.subagents import SubagentManager
from ..core.types import Message, MessageAttachment, ProviderEvent, ToolCall, Usage
from ..core.verify import Verifier, VerifyReport
from ..harness.memory import redact_secrets
from ..hooks import HookManager
from ..jobs import JobManager
from ..mcp.client import McpManager
from ..permissions.policy import (
    Approval,
    PermissionPolicy,
    PermissionRequest,
    is_risky_command,
)
from ..providers.base import ProviderError
from ..providers.profiles import load_profile
from ..providers.registry import RoleRouter
from ..sandbox import Sandbox
from ..tools.base import TodoItem, ToolContext, ToolRegistry, ToolResult

EventSink = Callable[[str, dict[str, Any]], Awaitable[None]]
PermissionResolver = Callable[[PermissionRequest, str], Awaitable[Approval]]
AskUser = Callable[[str], Awaitable[str]]

# Tools whose successful use counts as a state change for the completion
# self-check. `bash` is included because tests and verifiers often run through it.
STATE_CHANGING_TOOLS = {"edit_file", "write_file", "bash"}
FILE_CHANGE_TOOLS = {"edit_file", "write_file"}
FINISHED_TODO_STATUSES = {"done", "blocked"}
_MEMORY_CUE = re.compile(
    r"\b(ingat|inget|remember|selalu|always|jangan pernah|never|prefer|lebih suka|"
    r"suka|kebiasaan|convention)\b",
    re.IGNORECASE,
)

PLAN_TOOLS = {
    "read_file",
    "glob",
    "grep",
    "todo",
    "ask_user",
    "web_fetch",
    "skill_view",
    "recall",
    "repo_map",
    "symbols",
    "refs",
    "tool_output",
}


class RunResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_id: str
    text: str = ""
    steps: int = 0
    usage: UsageTotals = Field(default_factory=UsageTotals)
    aborted: bool = False
    error: str | None = None


def edit_diff(path: str, old: str, new: str) -> str:
    """Build a display-only unified diff with counts that match the hunk."""

    def prefixed(value: str, sign: str) -> str:
        lines = value.splitlines() or [""]
        return "\n".join(f"{sign}{line}" for line in lines)

    old_count = max(1, len(old.splitlines()))
    new_count = max(1, len(new.splitlines()))
    return (
        f"--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,{old_count} +1,{new_count} @@\n"
        f"{prefixed(old, '-')}\n{prefixed(new, '+')}\n"
    )


def _arguments_summary(name: str, arguments: dict[str, Any]) -> str:
    if name in {"read_file", "write_file", "edit_file", "list_dir"}:
        path = arguments.get("path")
        if isinstance(path, str) and path:
            return path
    if name == "bash":
        command = arguments.get("command")
        if isinstance(command, str) and command:
            return f"$ {command}"
    return json.dumps(arguments, ensure_ascii=False)[:500]


def _todos_text(todos: list[Any]) -> str:
    return "\n".join(f"[{item.status}] {item.id}: {item.content}" for item in todos)


def _restored_todos_text(todos: list[TodoItem]) -> str:
    counts: dict[str, int] = {}
    for item in todos:
        counts[item.status] = counts.get(item.status, 0) + 1
    summary = ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))
    return (
        "## Restored todo list\n\n"
        f"The previous turn left {len(todos)} todo items ({summary}). "
        "Call the todo tool to read their details before changing them. Keep the "
        "list current: mark finished items done, and update or remove items that "
        "are no longer relevant."
    )


def _explains_verifier_failure(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "pre-existing",
            "preexisting",
            "unrelated",
            "cannot fix",
            "not caused by",
            "known failure",
            "environment issue",
            "skipped",
        )
    )


async def _noop_emit(_event: ProviderEvent) -> None:
    return None


@dataclass
class _LoopToolContext(ToolContext):
    """Observe the real tool authorization without duplicating its request."""

    before_mutation: Callable[[PermissionRequest], Awaitable[None]] | None = None

    async def authorize(self, request: PermissionRequest) -> tuple[bool, str | None]:
        allowed, reason = await super().authorize(request)
        if allowed and self.before_mutation is not None:
            await self.before_mutation(request)
        return allowed, reason


class AgentLoop:
    def __init__(
        self,
        *,
        config: Config,
        store: Store,
        router: RoleRouter,
        tools: ToolRegistry,
    ) -> None:
        self.config = config
        self.store = store
        self.router = router
        self.tools = tools
        self._job_managers: dict[str, JobManager] = {}
        self._mcp_manager = McpManager.load()
        self._policies: dict[str, PermissionPolicy] = {}

    async def close_jobs(self) -> None:
        for manager in self._job_managers.values():
            await manager.close()
        self._job_managers.clear()
        await self._mcp_manager.close()

    async def mcp_status(self, *, refresh: bool = True) -> list[dict[str, Any]]:
        return await self._mcp_manager.status(refresh=refresh)

    async def restart_mcp(self, name: str) -> dict[str, Any]:
        return await self._mcp_manager.restart(name)

    def _profile(self, model_ref: str | None):
        if not model_ref or "/" not in model_ref:
            return None
        provider, _, model = model_ref.partition("/")
        try:
            return load_profile(provider, model)
        except (OSError, ValueError):
            return None

    def _tool_specs(self, mode: str):
        specs = self.tools.specs()
        if mode == "plan":
            return [spec for spec in specs if spec.name in PLAN_TOOLS]
        return specs

    async def run(
        self,
        session: Session,
        prompt: str,
        *,
        emit: EventSink,
        permission_resolver: PermissionResolver | None = None,
        ask_user: AskUser | None = None,
        model_override: str | None = None,
        role: str = "main",
        max_steps: int | None = None,
        effort: str | None = None,
        extend_budget: bool = False,
        attachments: list[MessageAttachment] | None = None,
        max_allowance: float | None = None,
    ) -> RunResult:
        if max_allowance is not None and (not math.isfinite(max_allowance) or max_allowance < 0):
            raise ConfigError("max_allowance must be finite and non-negative")
        starting_allowance = self.store.usage_totals(session.id).allowance

        def allowance_exhausted() -> bool:
            return max_allowance is not None and (
                self.store.usage_totals(session.id).allowance - starting_allowance >= max_allowance
            )

        workspace = Path(session.cwd).resolve()
        memory_cue = bool(_MEMORY_CUE.search(prompt))
        model_ref = model_override or session.model or self.config.roles.main
        session_effort = session.metadata.get("effort") if session.metadata else None
        effort = (
            effort
            or (session_effort if isinstance(session_effort, str) else None)
            or (self.config.agent.effort)
        )
        profile = self._profile(model_ref)
        policy = self._policies.get(session.id)
        if (
            policy is None
            or policy.workspace != workspace
            or policy.config != self.config.permissions
        ):
            policy = PermissionPolicy(workspace, self.config.permissions)
            self._policies[session.id] = policy
        context = _LoopToolContext(
            workspace=workspace,
            policy=policy,
            session_id=session.id,
            agent_config=self.config.agent,
            ask_user=ask_user,
            store=self.store,
            edit_format=profile.edit_format if profile else "search_replace",
            emit=emit,
            permission_resolver=permission_resolver,
        )
        restored_todos = self._load_todos(session)
        context.todos = restored_todos
        if self.config.subagents.enabled:
            context.subagent_manager = SubagentManager(
                config=self.config,
                store=self.store,
                router=self.router,
                tools=self.tools.tools(),
                parent_session=session,
                emit=emit,
                permission_resolver=permission_resolver,
                ask_user=ask_user,
            )
        job_manager = self._job_managers.get(session.id)
        if job_manager is None:
            job_manager = JobManager(self.config.jobs, cwd=workspace, emit=emit)
            self._job_managers[session.id] = job_manager
        context.jobs = job_manager
        context.hooks = HookManager.load()
        context.mcp = self._mcp_manager
        if self.config.sandbox.enabled:
            context.sandbox = Sandbox(self.config.sandbox, cwd=workspace)

        checkpointer: CheckpointManager | None = None
        if self.config.checkpoints.enabled:
            candidate = CheckpointManager(workspace, session.id, store=self.store)
            if await asyncio.to_thread(candidate.ensure):
                checkpointer = candidate
                context.checkpointer = candidate
        verifier: Verifier | None = None
        if self.config.verify.auto:
            verifier = Verifier(
                workspace,
                self.config.verify,
                authorize=context.authorize,
                sandbox=context.sandbox,
            )
            context.verifier = verifier
        context_manager = ContextManager(self.config.context)
        context_window = (
            profile.context_window
            if profile and profile.context_window
            else self.config.context.max_context_tokens
        )

        self.store.update_session(session.id, status="running")
        await emit("status", {"state": "running"})
        if restored_todos:
            await emit("todo.update", {"items": self._todos_payload(restored_todos)})

        user_message_id = f"msg_{uuid.uuid4().hex}"
        user_message = Message(role="user", content=prompt, attachments=attachments or [])
        self.store.append_message(session.id, user_message, message_id=user_message_id)
        self.store.append_trajectory(
            session.id,
            {
                "type": "user",
                "message_id": user_message_id,
                "content": prompt,
                "attachments": [item.mime for item in (attachments or [])],
            },
        )
        await emit(
            "message.start",
            {"message_id": user_message_id, "role": "user"},
        )
        await emit(
            "message.end",
            {"message_id": user_message_id, "finish_reason": "user"},
        )

        if checkpointer is not None:
            initial = await asyncio.to_thread(checkpointer.initial)
            if initial:
                await self._emit_checkpoint(initial, emit)

        messages = [
            Message(
                role="system",
                content=self._pinned_system_prompt(
                    session, workspace, model_ref, profile.edit_format if profile else None
                ),
            )
        ]
        messages.extend(self.store.get_context_messages(session.id))
        if context.hooks is not None:
            session_hook = await context.hooks.run(
                "SessionStart",
                {"session_id": session.id, "cwd": str(workspace), "model": model_ref},
            )
            if session_hook.context:
                messages.append(Message(role="system", content=session_hook.context))
        if restored_todos:
            messages.append(Message(role="system", content=_restored_todos_text(restored_todos)))
        tools = self._tool_specs(session.mode)
        steps = 0
        final_text = ""
        limit = (
            max_steps
            if max_steps is not None
            # getattr keeps an older daemon/config snapshot compatible with
            # this loop module during a partial preview upgrade.
            else (
                int(getattr(self.config.agent, "interactive_max_steps", 0) or 0)
                if extend_budget
                else self.config.agent.max_steps
            )
        )
        limit = limit if limit > 0 else None
        extension = max(1, self.config.agent.step_extension)
        ceiling = max(limit or 0, self.config.agent.max_steps_ceiling)
        successful_steps: deque[bool] = deque(maxlen=extension)
        signature_window: deque[str] = deque(maxlen=extension)
        window_has_file_change = False
        unproductive_steps = 0
        completion_nudges = 0
        self_check_done = False
        had_state_change = False
        stall_nudges = 0
        stall_limit = max(1, self.config.agent.stall_limit)
        stall_nudge_at = max(2, stall_limit // 2)
        compactions = 0
        consecutive_provider_failures = 0
        escalated = False

        try:
            while True:
                if allowance_exhausted():
                    return await self._allowance_stop(session, context, steps, emit)
                if limit is not None and steps >= limit:
                    min_unique = max(1, min(extension, 50) // 5)
                    productive_window = window_has_file_change or (
                        len(set(signature_window)) >= min_unique
                    )
                    if (
                        extend_budget
                        and limit < ceiling
                        and any(successful_steps)
                        and productive_window
                    ):
                        previous_limit = limit
                        limit = min(ceiling, limit + extension)
                        successful_steps.clear()
                        signature_window.clear()
                        window_has_file_change = False
                        await emit(
                            "budget.extended",
                            {
                                "previous_limit": previous_limit,
                                "new_limit": limit,
                                "ceiling": ceiling,
                                "steps": steps,
                            },
                        )
                        continue
                    break
                steps += 1
                step_id = f"step_{steps}"
                pruned = context_manager.prune(messages)
                state = context_manager.state(
                    messages,
                    system_prompt=messages[0].content or "",
                    tools=tools,
                    context_window=context_window,
                    pruned=pruned,
                )
                if compactions < 3 and context_manager.should_compact(state.tokens, context_window):
                    messages, usage = await self._compact(
                        session, messages, context, context_manager, emit
                    )
                    compactions += 1
                    if usage.input_tokens or usage.output_tokens or usage.allowance:
                        self.store.record_usage(
                            session_id=session.id,
                            step_id="compact",
                            provider=None,
                            model=model_ref,
                            usage=usage,
                        )
                        await emit(
                            "usage",
                            {
                                "step_id": "compact",
                                "model": model_ref,
                                **usage.model_dump(mode="json", exclude={"raw"}),
                                "session_allowance": self.store.usage_totals(session.id).allowance,
                            },
                        )
                    state = context_manager.state(
                        messages,
                        system_prompt=messages[0].content or "",
                        tools=tools,
                        context_window=context_window,
                        pruned=pruned,
                        compacted=True,
                    )
                if allowance_exhausted():
                    return await self._allowance_stop(session, context, steps, emit)
                await emit(
                    "context",
                    {
                        "tokens": state.tokens,
                        "percent": state.percent,
                        "context_window": context_window,
                        "pruned": state.pruned,
                        "compacted": state.compacted,
                    },
                )

                assistant_id = f"msg_{uuid.uuid4().hex}"
                await emit(
                    "message.start",
                    {"message_id": assistant_id, "role": "assistant", "step_id": step_id},
                )

                async def provider_emit(
                    event: ProviderEvent,
                    _assistant_id: str = assistant_id,
                    _step_id: str = step_id,
                ) -> None:
                    if event.type == "text.delta" and event.text:
                        await emit(
                            "message.delta",
                            {
                                "message_id": _assistant_id,
                                "step_id": _step_id,
                                "text": event.text,
                            },
                        )
                    elif event.type == "reasoning.delta" and event.text:
                        await emit(
                            "reasoning.delta",
                            {
                                "message_id": _assistant_id,
                                "step_id": _step_id,
                                "text": event.text,
                            },
                        )
                    elif event.type == "retry" and event.retry is not None:
                        await emit("provider.retry", event.retry.model_dump(mode="json"))
                    elif event.type == "reset":
                        await emit("message.reset", {"message_id": _assistant_id})

                try:
                    provider_started = time.perf_counter()
                    outcome = await self.router.stream(
                        role,
                        messages=messages,
                        tools=tools,
                        session_id=session.id,
                        emit=provider_emit,
                        model_override=model_override,
                        temperature=0.0,
                        effort=effort,
                    )
                    latency_ms = (time.perf_counter() - provider_started) * 1000.0
                except ProviderError as exc:
                    consecutive_provider_failures += 1
                    escalation = self.config.escalation
                    current_model = model_override or session.model or self.config.roles.main
                    if (
                        escalation.enabled
                        and not escalated
                        and escalation.model
                        and escalation.model != current_model
                        and consecutive_provider_failures >= escalation.after_failures
                    ):
                        escalated = True
                        consecutive_provider_failures = 0
                        model_override = escalation.model
                        await emit(
                            "escalation",
                            {
                                "from": current_model,
                                "to": escalation.model,
                                "reason": exc.code,
                                "failures": escalation.after_failures,
                            },
                        )
                        continue
                    await emit(
                        "error",
                        {
                            "code": exc.code,
                            "message": str(exc),
                            "retryable": exc.retryable,
                            "details": {"status": exc.status},
                        },
                    )
                    await self._session_hooks(context, "SessionEnd", "error")
                    await self._session_hooks(context, "Stop", "error")
                    self.store.update_session(session.id, status="error")
                    await self._sync_todos(session, context, emit)
                    await emit("run.end", {"reason": "error", "error": str(exc)})
                    return RunResult(
                        session_id=session.id,
                        text=final_text,
                        steps=steps,
                        usage=self.store.usage_totals(session.id),
                        error=str(exc),
                    )

                # Provider call ids are untrusted and only provider-local.
                # Allocate durable, filesystem-safe identities before any event,
                # persistence, tool execution, or output offload uses them.
                outcome = outcome.model_copy(
                    update={
                        "tool_calls": [
                            call.model_copy(update={"id": f"call_{uuid.uuid4().hex}"})
                            for call in outcome.tool_calls
                        ]
                    }
                )
                self.store.update_session(session.id, model=f"{outcome.provider}/{outcome.model}")
                assistant_message = Message(
                    role="assistant",
                    content=outcome.text or None,
                    reasoning=outcome.reasoning or None,
                    tool_calls=outcome.tool_calls,
                )
                self.store.append_message(session.id, assistant_message, message_id=assistant_id)
                self.store.record_usage(
                    session_id=session.id,
                    step_id=step_id,
                    provider=outcome.provider,
                    model=outcome.model,
                    usage=outcome.usage,
                )
                if self.config.trace.enabled:
                    self.store.record_trace(
                        session_id=session.id,
                        step_id=step_id,
                        provider=outcome.provider,
                        model=outcome.model,
                        latency_ms=latency_ms,
                        usage=outcome.usage,
                        tool_names=[call.name for call in outcome.tool_calls],
                    )
                self.store.append_trajectory(
                    session.id,
                    {
                        "type": "assistant",
                        "message_id": assistant_id,
                        "step_id": step_id,
                        "text": outcome.text,
                        "tool_calls": [call.model_dump() for call in outcome.tool_calls],
                        "usage": outcome.usage.model_dump(mode="json"),
                        "finish_reason": outcome.finish_reason,
                    },
                )
                await emit(
                    "message.end",
                    {
                        "message_id": assistant_id,
                        "step_id": step_id,
                        "finish_reason": outcome.finish_reason,
                    },
                )
                await emit(
                    "usage",
                    {
                        "step_id": step_id,
                        "model": f"{outcome.provider}/{outcome.model}",
                        **outcome.usage.model_dump(mode="json", exclude={"raw"}),
                        "session_allowance": self.store.usage_totals(session.id).allowance,
                    },
                )
                messages.append(assistant_message)
                final_text = outcome.text or final_text
                if allowance_exhausted():
                    return await self._allowance_stop(
                        session,
                        context,
                        steps,
                        emit,
                        pending_calls=outcome.tool_calls,
                        assistant_message_id=assistant_id,
                    )

                if not outcome.tool_calls:
                    if session.mode == "plan":
                        plan_path = self._write_plan(workspace, final_text)
                        current = self.store.get_session(session.id)
                        metadata = dict(current.metadata) if current else {}
                        metadata["plan_path"] = str(plan_path)
                        metadata["plan_updated_at"] = datetime.now(UTC).isoformat()
                        self.store.update_session(session.id, metadata=metadata)
                        await emit(
                            "plan",
                            {
                                "path": str(plan_path),
                                "content": final_text[:8000],
                            },
                        )
                        return await self._complete(session, context, final_text, steps, emit)

                    if context.verify_failed:
                        if context.verify_repair_attempts > 0 and _explains_verifier_failure(
                            final_text
                        ):
                            await emit(
                                "verifier",
                                {
                                    "status": "accepted_explanation",
                                    "summary": (
                                        context.verify_report.summary
                                        if context.verify_report
                                        else ""
                                    ),
                                },
                            )
                        elif context.verify_repair_attempts < self.config.verify.max_repair_steps:
                            context.verify_repair_attempts += 1
                            instruction = Message(
                                role="system",
                                content=(
                                    "The verifier reported a new failure after your last edit. "
                                    "Fix it and run the verifier again. If the failure is "
                                    "pre-existing or unrelated, say so explicitly using the word "
                                    "'pre-existing' or 'unrelated'. Do not claim completion "
                                    "otherwise.\n\n"
                                    + (
                                        context.verify_report.summary
                                        if context.verify_report
                                        else ""
                                    )
                                ),
                            )
                            self.store.append_message(session.id, instruction)
                            messages.append(instruction)
                            await emit(
                                "verifier",
                                {
                                    "status": "retry",
                                    "attempt": context.verify_repair_attempts,
                                    "summary": (
                                        context.verify_report.summary
                                        if context.verify_report
                                        else ""
                                    ),
                                },
                            )
                            continue
                        else:
                            self.store.update_session(session.id, status="error")
                            summary = context.verify_report.summary if context.verify_report else ""
                            await emit(
                                "error",
                                {
                                    "code": "verification_failed",
                                    "message": (
                                        "The verifier is still failing; the run did not claim "
                                        "success"
                                    ),
                                    "retryable": False,
                                    "details": {"summary": summary[:4000]},
                                },
                            )
                            await emit(
                                "run.end",
                                {
                                    "reason": "verification_failed",
                                    "error": "verification_failed",
                                },
                            )
                            return RunResult(
                                session_id=session.id,
                                text=final_text,
                                steps=steps,
                                usage=self.store.usage_totals(session.id),
                                error="verification_failed",
                            )

                    nudge = self._completion_nudge(
                        context=context,
                        current_text=outcome.text or "",
                        steps=steps,
                        completion_nudges=completion_nudges,
                        self_check_done=self_check_done,
                        had_state_change=had_state_change,
                        memory_cue=memory_cue,
                    )
                    min_unique = max(1, min(extension, 50) // 5)
                    can_extend = (
                        limit is not None
                        and extend_budget
                        and limit < ceiling
                        and any(successful_steps)
                        and (window_has_file_change or len(set(signature_window)) >= min_unique)
                    )
                    can_continue = limit is None or steps < limit or can_extend
                    if (
                        nudge is not None
                        and can_continue
                        and completion_nudges < max(0, self.config.agent.max_completion_nudges)
                    ):
                        message, reason = nudge
                        completion_nudges += 1
                        if reason == "self_check":
                            self_check_done = True
                        instruction = Message(role="system", content=message)
                        self.store.append_message(session.id, instruction)
                        messages.append(instruction)
                        unfinished = [
                            item.id
                            for item in context.todos
                            if item.status not in FINISHED_TODO_STATUSES
                        ]
                        await emit(
                            "completion.check",
                            {
                                "status": "retry",
                                "reason": reason,
                                "attempt": completion_nudges,
                                "unfinished": unfinished,
                                "steps": steps,
                            },
                        )
                        continue

                    unfinished = [
                        item.id
                        for item in context.todos
                        if item.status not in FINISHED_TODO_STATUSES
                    ]
                    if unfinished:
                        await emit(
                            "completion.check",
                            {"status": "unfinished", "unfinished": unfinished},
                        )
                    return await self._complete(session, context, final_text, steps, emit)

                step_succeeded = False
                step_changed_state = False
                step_changed_files = False
                step_signature = "|".join(
                    f"{call.name}:{json.dumps(call.arguments, sort_keys=True, default=str)}"
                    for call in outcome.tool_calls
                )
                for call in outcome.tool_calls:
                    result = await self._run_tool_call(
                        session=session,
                        context=context,
                        call=call,
                        messages=messages,
                        emit=emit,
                        permission_resolver=permission_resolver,
                        assistant_message_id=assistant_id,
                    )
                    step_succeeded = step_succeeded or result.ok
                    delegated_write = call.name == "task" and bool(
                        (result.data or {}).get("writable")
                    )
                    if (result.ok and call.name in STATE_CHANGING_TOOLS) or delegated_write:
                        step_changed_state = True
                    if (result.ok and call.name in FILE_CHANGE_TOOLS) or delegated_write:
                        step_changed_files = True
                successful_steps.append(step_succeeded)
                signature_window.append(step_signature)
                window_has_file_change = window_has_file_change or step_changed_files
                unproductive_steps = 0 if step_succeeded else unproductive_steps + 1
                had_state_change = had_state_change or step_changed_state
                await self._persist_todos(session, context)

                if extend_budget and unproductive_steps >= stall_limit:
                    await self._sync_todos(session, context, emit)
                    await self._session_hooks(context, "SessionEnd", "stalled")
                    await self._session_hooks(context, "Stop", "stalled")
                    self.store.update_session(session.id, status="idle")
                    await emit(
                        "error",
                        {
                            "code": "stalled",
                            "message": (
                                f"No tool call succeeded for {unproductive_steps} consecutive "
                                "steps; stopping to avoid wasting the budget. Change the "
                                "approach or raise agent.stall_limit."
                            ),
                            "retryable": False,
                        },
                    )
                    await emit("run.end", {"reason": "stalled"})
                    return RunResult(
                        session_id=session.id,
                        text=final_text,
                        steps=steps,
                        usage=self.store.usage_totals(session.id),
                        error="stalled",
                    )

                if extend_budget and unproductive_steps == stall_nudge_at and stall_nudges < 2:
                    stall_nudges += 1
                    instruction = Message(
                        role="system",
                        content=(
                            "Several recent tool calls failed or produced no useful result. "
                            "Inspect the error, change the approach, and do not repeat the same "
                            "failing call unchanged."
                        ),
                    )
                    self.store.append_message(session.id, instruction)
                    messages.append(instruction)
                    await emit(
                        "completion.check",
                        {"status": "stall", "attempt": stall_nudges, "steps": steps},
                    )

            await self._sync_todos(session, context, emit)
            await self._session_hooks(context, "SessionEnd", "max_steps")
            await self._session_hooks(context, "Stop", "max_steps")
            self.store.update_session(session.id, status="idle")
            await emit(
                "error",
                {
                    "code": "max_steps",
                    "message": (
                        f"Step limit reached ({steps} steps). The run did not finish; "
                        "continue with another message or raise the applicable budget "
                        "(agent.max_steps, agent.interactive_max_steps, or --max-steps)."
                    ),
                    "retryable": False,
                    "details": {"steps": steps, "limit": limit},
                },
            )
            await emit("run.end", {"reason": "max_steps"})
            return RunResult(
                session_id=session.id,
                text=final_text,
                steps=steps,
                usage=self.store.usage_totals(session.id),
                error="max_steps",
            )
        except asyncio.CancelledError:
            await self._session_hooks(context, "SessionEnd", "aborted")
            await self._session_hooks(context, "Stop", "aborted")
            self.store.update_session(session.id, status="idle")
            await self._sync_todos(session, context, emit)
            self.store.append_trajectory(session.id, {"type": "cancelled"})
            await emit("run.end", {"reason": "aborted"})
            raise
        except Exception as exc:  # keep the session consistent on unexpected errors
            await self._session_hooks(context, "SessionEnd", "error")
            await self._session_hooks(context, "Stop", "error")
            self.store.update_session(session.id, status="error")
            await self._sync_todos(session, context, emit)
            await emit("error", {"code": "internal_error", "message": str(exc), "retryable": False})
            await emit("run.end", {"reason": "error", "error": str(exc)})
            return RunResult(
                session_id=session.id,
                text=final_text,
                steps=steps,
                usage=self.store.usage_totals(session.id),
                error=str(exc),
            )

    def _pinned_system_prompt(
        self,
        session: Session,
        workspace: Path,
        model_ref: str | None,
        edit_format: str | None,
    ) -> str:
        current = self.store.get_session(session.id)
        metadata = dict(current.metadata) if current else {}
        saved = metadata.get("pinned_system_prompts")
        prompts = dict(saved) if isinstance(saved, dict) else {}
        key = json.dumps([model_ref, session.mode, edit_format])
        if isinstance(prompts.get(key), str):
            return prompts[key]
        # Render both modes together so a later plan/build switch does not
        # accidentally activate a promoted prompt in an existing session.
        for mode in {"build", "plan", session.mode}:
            mode_key = json.dumps([model_ref, mode, edit_format])
            if mode_key not in prompts:
                prompts[mode_key] = redact_secrets(
                    system_prompt(workspace, model_ref, mode=mode, edit_format=edit_format)
                )
        metadata["pinned_system_prompts"] = prompts
        self.store.update_session(session.id, metadata=metadata)
        return prompts[key]

    async def _allowance_stop(
        self,
        session: Session,
        context: ToolContext,
        steps: int,
        emit: EventSink,
        *,
        pending_calls: list[ToolCall] | None = None,
        assistant_message_id: str | None = None,
    ) -> RunResult:
        # Complete the tool-call protocol even though no tool may execute after
        # the charged response reaches the cap. This also permits safe resume.
        reason = "Allowance limit reached; remaining tool calls were not executed."
        for call in pending_calls or []:
            result = ToolResult.failure(reason)
            self.store.record_tool_call(
                session_id=session.id,
                tool_call_id=call.id,
                message_id=assistant_message_id,
                name=call.name,
                arguments=call.arguments,
                result=result.model_dump(mode="json"),
                status="error",
            )
            self.store.append_message(
                session.id, Message(role="tool", content=reason, tool_call_id=call.id)
            )
            self.store.append_trajectory(
                session.id,
                {
                    "type": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "result": result.model_dump(mode="json"),
                },
            )
            await emit(
                "tool.end",
                {
                    "message_id": assistant_message_id,
                    "tool_call_id": call.id,
                    "name": call.name,
                    "status": "error",
                    "error": reason,
                    "output": reason,
                },
            )
        await self._sync_todos(session, context, emit)
        await self._session_hooks(context, "SessionEnd", "allowance_exceeded")
        await self._session_hooks(context, "Stop", "allowance_exceeded")
        self.store.update_session(session.id, status="error")
        await emit("error", {"code": "allowance_exceeded", "message": reason, "retryable": False})
        await emit("run.end", {"reason": "allowance_exceeded", "error": "allowance_exceeded"})
        return RunResult(
            session_id=session.id,
            steps=steps,
            usage=self.store.usage_totals(session.id),
            error="allowance_exceeded",
        )

    async def _session_hooks(self, context: ToolContext, event: str, reason: str) -> None:
        if context.hooks is None:
            return
        await context.hooks.run(
            event,
            {"session_id": context.session_id, "cwd": str(context.workspace), "reason": reason},
        )

    async def _complete(
        self,
        session: Session,
        context: ToolContext,
        final_text: str,
        steps: int,
        emit: EventSink,
    ) -> RunResult:
        await self._session_hooks(context, "SessionEnd", "completed")
        await self._session_hooks(context, "Stop", "completed")
        self.store.update_session(session.id, status="idle")
        # The agent declared the run complete. An item still marked in_progress
        # was being worked on when the agent stopped; pending items are not
        # silently promoted, so incomplete plans remain visible.
        for item in context.todos:
            if item.status == "in_progress":
                item.status = "done"
        await self._sync_todos(session, context, emit)
        await emit("status", {"state": "idle"})
        await emit("run.end", {"reason": "completed"})
        return RunResult(
            session_id=session.id,
            text=final_text,
            steps=steps,
            usage=self.store.usage_totals(session.id),
        )

    async def _compact(
        self,
        session: Session,
        messages: list[Message],
        context: ToolContext,
        context_manager: ContextManager,
        emit: EventSink,
    ) -> tuple[list[Message], Usage]:
        current = self.store.get_session(session.id)
        previous = ""
        if current is not None:
            previous = str((current.metadata or {}).get("context_summary") or "").strip()
        todo_text = _todos_text(context.todos)
        summary = fallback_summary(messages[1:], todo_text)
        if previous:
            # A later compaction must not drop the goal/decisions captured by an
            # earlier one (manual /compact already behaves this way).
            summary = f"## Previous context\n{previous}\n\n{summary}"
        usage = Usage()
        if self.config.roles.fast:
            try:
                outcome = await self.router.stream(
                    "fast",
                    messages=compaction_prompt(messages[1:], todo_text, previous_summary=previous),
                    tools=[],
                    session_id=session.id,
                    emit=_noop_emit,
                    temperature=0.0,
                    max_tokens=1200,
                )
                usage = outcome.usage
                if outcome.text and outcome.text.strip():
                    summary = outcome.text.strip()
            except (ConfigError, ProviderError):
                pass
        summary = summary[: self.config.context.summary_max_chars]
        through = self.store.max_message_seq(session.id)
        self.store.set_context_summary(session.id, summary, through)
        system_message = messages[0]
        compacted = context_manager.apply_summary(messages[1:], summary)
        merged = [system_message, *compacted]
        await emit(
            "context.compact",
            {
                "through_seq": through,
                "summary": summary[:4000],
                "tokens": context_manager.state(
                    merged,
                    system_prompt=system_message.content or "",
                    tools=[],
                    context_window=None,
                ).tokens,
            },
        )
        return merged, usage

    def _write_plan(self, workspace: Path, text: str) -> Path:
        path = workspace / ".siwah" / "plan.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        body = (
            "# Plan\n\n"
            "<!-- Generated by siwah. Edit this file, then switch to build mode and "
            "run /execute-plan. -->\n\n"
            f"{text.strip()}\n"
        )
        temporary = path.with_suffix(".tmp")
        temporary.write_text(body, encoding="utf-8")
        temporary.replace(path)
        return path

    async def _emit_checkpoint(self, checkpoint_id: str, emit: EventSink) -> None:
        record = self.store.get_checkpoint(checkpoint_id)
        if record:
            await emit(
                "checkpoint",
                {
                    "id": record["id"],
                    "label": record.get("label"),
                    "commit": record.get("commit_hash"),
                    "created_at": record.get("created_at"),
                },
            )

    async def _run_tool_call(
        self,
        *,
        session: Session,
        context: _LoopToolContext,
        call: ToolCall,
        messages: list[Message],
        emit: EventSink,
        permission_resolver: PermissionResolver | None,
        assistant_message_id: str,
    ) -> ToolResult:
        started = time.perf_counter()
        await emit(
            "tool.start",
            {
                "message_id": assistant_message_id,
                "tool_call_id": call.id,
                "name": call.name,
                "arguments_summary": _arguments_summary(call.name, call.arguments),
            },
        )
        if session.mode == "plan" and call.name not in PLAN_TOOLS:
            result = ToolResult.failure(
                f"Tool {call.name} is unavailable in plan mode; switch to build mode first."
            )
        elif not call.arguments_valid:
            result = ToolResult.failure(
                "Invalid tool-call JSON arguments. Re-emit the tool call with valid JSON."
            )
        else:

            async def approver(request: PermissionRequest) -> Approval:
                request_id = f"perm_{uuid.uuid4().hex}"
                await emit(
                    "permission.request",
                    {
                        "request_id": request_id,
                        "tool_call_id": call.id,
                        "tool": request.tool,
                        "kind": request.kind,
                        "preview": request.preview,
                        "patterns": request.patterns,
                        "choices": ["once", "session", "always", "deny"],
                    },
                )
                if permission_resolver is None:
                    approval = Approval(decision="deny", reason="No approver available")
                else:
                    approval = await permission_resolver(request, request_id)
                await emit(
                    "permission.resolved",
                    {
                        "request_id": request_id,
                        "decision": approval.decision,
                        "reason": approval.reason,
                    },
                )
                return approval

            context.approver = approver
            context.tool_call_id = call.id

            async def before_mutation(request: PermissionRequest) -> None:
                if request.tool != call.name:
                    return
                if (
                    request.kind == "bash"
                    and call.name == "bash"
                    and context.checkpointer is not None
                    and is_risky_command(request.command or "")
                ):
                    checkpoint_id = await asyncio.to_thread(
                        context.checkpointer.snapshot, "before risky bash", allow_empty=True
                    )
                    if checkpoint_id:
                        await self._emit_checkpoint(checkpoint_id, emit)
                if (
                    request.kind == "write"
                    and call.name in {"edit_file", "write_file", "task"}
                    and context.verifier is not None
                    and context.verify_baseline is None
                ):
                    await emit("verifier", {"status": "baseline"})
                    context.verify_baseline = await context.verifier.run()

            pre_hook = None
            if context.hooks is not None:
                pre_hook = await context.hooks.run(
                    "PreToolUse",
                    {
                        "session_id": session.id,
                        "tool": call.name,
                        "arguments": call.arguments,
                        "cwd": str(context.workspace),
                    },
                )
            if pre_hook is not None and pre_hook.allow is False:
                result = ToolResult.failure(pre_hook.reason or "Denied by PreToolUse hook")
            else:
                context.before_mutation = before_mutation
                try:
                    result = await self.tools.execute(call.name, context, call.arguments)
                finally:
                    context.before_mutation = None
            if context.hooks is not None:
                hook_result = await context.hooks.run(
                    "PostToolUse",
                    {
                        "session_id": session.id,
                        "tool": call.name,
                        "arguments": call.arguments,
                        "ok": result.ok,
                        "output": result.output[:4_000],
                    },
                )
                if hook_result.context:
                    result = result.model_copy(
                        update={"output": result.output + "\n\n" + hook_result.context}
                    )
            if context.verifier is not None and (
                (call.name in {"edit_file", "write_file"} and result.ok)
                or (call.name == "task" and (result.data or {}).get("writable"))
            ):
                await emit("verifier", {"status": "running"})
                report: VerifyReport = await context.verifier.run()
                context.verify_report = report
                failures = report.new_failures(context.verify_baseline)
                # Unchanged baseline failures and denied checks still require
                # an honest explanation; neither means "all checks passed".
                context.verify_failed = report.status == "failed"
                data = dict(result.data or {})
                result = result.model_copy(
                    update={
                        "output": result.output + "\n\n" + report.summary,
                        "data": data,
                    }
                )
                await emit(
                    "verifier",
                    {
                        "status": report.status,
                        "summary": report.summary[:6000],
                        "new_failures": [item.name for item in failures],
                    },
                )
            if context.checkpointer is not None and call.name in {
                "write_file",
                "edit_file",
                "bash",
                "task",
            }:
                checkpoint_id = await asyncio.to_thread(
                    context.checkpointer.snapshot, f"{call.name}:{call.id}"
                )
                if checkpoint_id:
                    await self._emit_checkpoint(checkpoint_id, emit)

        if call.name == "memory_write" and result.ok:
            context.memory_writes += 1
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        status = "ok" if result.ok else "error"
        self.store.record_tool_call(
            session_id=session.id,
            tool_call_id=call.id,
            message_id=assistant_message_id,
            name=call.name,
            arguments=call.arguments,
            result=result.model_dump(mode="json"),
            status=status,
        )
        self.store.append_trajectory(
            session.id,
            {
                "type": "tool",
                "tool_call_id": call.id,
                "name": call.name,
                "arguments": call.arguments,
                "result": result.model_dump(mode="json"),
                "duration_ms": duration_ms,
            },
        )
        tool_message = Message(
            role="tool", content=redact_secrets(result.output), tool_call_id=call.id
        )
        self.store.append_message(session.id, tool_message)
        messages.append(tool_message)
        data = result.data or {}
        full_output = data.get("full_output") if isinstance(data, dict) else None
        output_for_client = full_output if isinstance(full_output, str) else result.output
        await emit(
            "tool.end",
            {
                "message_id": assistant_message_id,
                "tool_call_id": call.id,
                "name": call.name,
                "status": status,
                "duration_ms": duration_ms,
                "output": output_for_client,
                "output_summary": result.output[:2000],
                "output_handle": data.get("handle") if isinstance(data, dict) else None,
                "edited": bool(data.get("edited")) if isinstance(data, dict) else False,
                "error": result.error,
            },
        )
        if call.name == "todo":
            await emit(
                "todo.update",
                {"items": [item.model_dump(mode="json") for item in context.todos]},
            )
        if call.name in {"edit_file", "write_file"} and result.ok:
            patch = data.get("diff") if isinstance(data, dict) else None
            if (not isinstance(patch, str) or not patch) and call.name == "edit_file":
                old = str(call.arguments.get("old_string", ""))
                new = str(call.arguments.get("new_string", ""))
                path = str(call.arguments.get("path", ""))
                patch = edit_diff(path, old, new)
            # write_file of a new (or unchanged) file has no patch. Synthesizing
            # one from empty old/new strings produced a bogus empty +/- card.
            if isinstance(patch, str) and patch:
                await emit(
                    "diff",
                    {
                        "path": str(call.arguments.get("path", "")),
                        "patch": patch,
                        "view_hint": "auto",
                    },
                )
        return result

    def _load_todos(self, session: Session) -> list[TodoItem]:
        current = self.store.get_session(session.id)
        raw = (current.metadata or {}).get("todos") if current else None
        if not isinstance(raw, list):
            return []
        todos: list[TodoItem] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            status = str(item.get("status") or "pending")
            if status not in {"pending", "in_progress", "done", "blocked"}:
                status = "pending"
            todos.append(
                TodoItem(
                    id=str(item.get("id") or index + 1),
                    content=content,
                    status=status,
                )
            )
        return todos

    @staticmethod
    def _todos_payload(todos: list[TodoItem]) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in todos]

    async def _sync_todos(self, session: Session, context: ToolContext, emit: EventSink) -> None:
        current = self.store.get_session(session.id)
        metadata = dict(current.metadata) if current else {}
        metadata["todos"] = self._todos_payload(context.todos)
        self.store.update_session(session.id, metadata=metadata)
        if context.todos:
            await emit("todo.update", {"items": metadata["todos"]})

    async def _persist_todos(self, session: Session, context: ToolContext) -> None:
        current = self.store.get_session(session.id)
        metadata = dict(current.metadata) if current else {}
        metadata["todos"] = self._todos_payload(context.todos)
        self.store.update_session(session.id, metadata=metadata)

    def _completion_nudge(
        self,
        *,
        context: ToolContext,
        current_text: str,
        steps: int,
        completion_nudges: int,
        self_check_done: bool,
        had_state_change: bool,
        memory_cue: bool,
    ) -> tuple[str, str] | None:
        unfinished = [item for item in context.todos if item.status not in FINISHED_TODO_STATUSES]
        if unfinished:
            listing = ", ".join(f"{item.id} ({item.status})" for item in unfinished)
            return (
                "Completion check: the todo list still has unfinished items: "
                f"{listing}.\n\n"
                "Call the todo tool to read their details. Continue working on them now, "
                "and mark every finished item done. If an item cannot be finished, mark it "
                "blocked and explain why in the final answer. Do not give the final answer "
                "while an item is pending or in_progress.",
                "todos",
            )
        if not current_text.strip():
            return (
                "Your last turn produced no answer and no tool calls. Continue the task, or "
                "give the final answer if the work is actually complete.",
                "empty",
            )
        if memory_cue and context.memory_writes == 0:
            return (
                "The user expressed a durable preference or asked you to remember something. "
                "Before finishing, call memory_write with target 'user' (or 'project' for "
                "repository facts). If it is not durable, explain why.",
                "memory",
            )
        if (
            completion_nudges == 0
            and not self_check_done
            and had_state_change
            and steps >= self.config.agent.self_check_min_steps
        ):
            return (
                "Before finalizing, self-check the deliverable against the user's request. "
                "Use the tool results as evidence, confirm that every requested change is "
                "present, and run the relevant verification if you have not already. If "
                "everything is complete, reply with the final answer; otherwise continue "
                "working.",
                "self_check",
            )
        return None
