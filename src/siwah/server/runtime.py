"""Daemon runtime: sessions, event buses, runs, and permission waiters."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import threading
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..adapt.trigger import EvolutionTrigger
from ..auth import AuthStore
from ..config import Config, ConfigError, update_agent_effort
from ..core.checkpoints import CheckpointError, CheckpointManager
from ..core.context import (
    compaction_prompt,
    estimate_tokens,
    fallback_summary,
)
from ..core.loop import AgentLoop
from ..core.storage import Session, Store
from ..core.types import MessageAttachment, ModelInfo, Usage
from ..harness.memory import init_agents_file, init_project_memory, quick_note, read_memory
from ..harness.skills import list_skills, read_skill
from ..permissions.policy import Approval, PermissionPolicy, PermissionRequest
from ..providers.base import ProviderError
from ..providers.profiles import load_profile
from ..providers.registry import ProviderRegistry, RoleRouter
from ..sandbox import Sandbox
from ..tools.base import ToolContext, ToolRegistry
from ..tools.runtime import BashTool, default_tools
from .events import EventBus

PERMISSION_TIMEOUT_S = 300.0
MAX_QUEUED_PROMPTS = 16


@dataclass(frozen=True)
class QueuedPrompt:
    content: str
    attachments: list[MessageAttachment]


class RuntimeError_(RuntimeError):
    pass


class ShellPermissionRequired(RuntimeError):
    def __init__(self, preview: str) -> None:
        super().__init__("Shell command requires approval")
        self.preview = preview


class ShellDenied(RuntimeError):
    pass


class ConfirmationRequired(RuntimeError):
    def __init__(self, checkpoint: dict[str, Any]) -> None:
        super().__init__("Confirmation required before rewinding the workspace")
        self.checkpoint = checkpoint


class DaemonRuntime:
    def __init__(
        self,
        config: Config,
        *,
        auth: AuthStore | None = None,
        store: Store | None = None,
        registry: ProviderRegistry | None = None,
        router: RoleRouter | None = None,
        loop: AgentLoop | None = None,
    ) -> None:
        self.config = config
        self.store = store or Store()
        self.auth = auth or AuthStore()
        self.registry = registry or ProviderRegistry(config, self.auth)
        self.router = router or RoleRouter(config, self.registry)
        self.loop = loop or AgentLoop(
            config=config,
            store=self.store,
            router=self.router,
            tools=ToolRegistry(default_tools()),
        )
        self.evolution_trigger = EvolutionTrigger(
            config.evolution,
            enabled=bool(config.evolution.auto and (config.roles.reflector or config.roles.main)),
        )
        self.started_at = datetime.now(UTC).isoformat()
        self._buses: dict[str, EventBus] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._queues: dict[str, deque[QueuedPrompt]] = {}
        self._waiters: dict[str, asyncio.Future[Approval]] = {}
        self._waiter_sessions: dict[str, str] = {}
        self._watermarks: dict[str, int] = {}
        self._operation_lock = threading.RLock()
        self._workspace_operations: set[Path] = set()

    # ------------------------------------------------------------------
    # Sessions and runs
    # ------------------------------------------------------------------
    def create_session(
        self,
        *,
        cwd: str | None,
        model: str | None,
        mode: str = "build",
        title: str | None = None,
    ) -> Session:
        workspace = Path(cwd or Path.cwd()).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError(f"Workspace does not exist: {workspace}")
        if model is None and self.config.roles.main is not None:
            model = self.config.roles.main
        return self.store.create_session(cwd=str(workspace), model=model, mode=mode, title=title)

    def get_session(self, session_id: str) -> Session | None:
        return self.store.get_session(session_id)

    def is_running(self, session_id: str) -> bool:
        task = self._tasks.get(session_id)
        return task is not None and not task.done()

    def list_sessions(self, limit: int = 50) -> list[Session]:
        return self.store.list_sessions(limit=limit)

    def bus(self, session_id: str) -> EventBus:
        return self._buses.setdefault(
            session_id, EventBus(session_id, retention=self.config.server.event_retention)
        )

    def last_event_seq(self, session_id: str) -> int:
        bus = self._buses.get(session_id)
        if bus is None:
            return 0
        task = self._tasks.get(session_id)
        if task is not None and not task.done():
            # Message deltas are streamed before the assistant row exists; a
            # watermark past them would make hydrate + replay lossy.
            return self._watermarks.get(session_id, 0)
        return bus.last_seq()

    def start_run(
        self,
        session: Session,
        content: str,
        attachments: list[MessageAttachment] | None = None,
    ) -> tuple[str, int]:
        with self._operation_lock:
            workspace = Path(session.cwd).resolve()
            if any(_overlaps(workspace, root) for root in self._workspace_operations):
                raise SessionBusy("Workspace is busy with another operation")
            return self._start_run(session, content, attachments)

    def submit_run(
        self,
        session: Session,
        content: str,
        attachments: list[MessageAttachment] | None = None,
    ) -> tuple[str, int, str]:
        """Start a run, or queue it behind the active run for this session."""

        with self._operation_lock:
            if not self.is_running(session.id):
                run_id, event_id = self.start_run(session, content, attachments)
                return run_id, event_id, "running"
            queue = self._queues.setdefault(session.id, deque())
            if len(queue) >= MAX_QUEUED_PROMPTS:
                raise RuntimeError_(
                    f"At most {MAX_QUEUED_PROMPTS} prompts can be queued for a session"
                )
            queue.append(QueuedPrompt(content=content, attachments=list(attachments or [])))
            run_id = f"queued_{uuid.uuid4().hex}"
            return run_id, self.bus(session.id).last_seq(), "queued"

    def _start_run(
        self,
        session: Session,
        content: str,
        attachments: list[MessageAttachment] | None = None,
    ) -> tuple[str, int]:
        if not session.cwd:
            raise ValueError("Session has no workspace")
        existing = self._tasks.get(session.id)
        if existing is not None and not existing.done():
            raise RuntimeError_("A run is already active for this session")
        if not session.title:
            title = content.strip().splitlines()[0][:80] if content.strip() else "Image message"
            if title:
                self.store.update_session(session.id, title=title)
        run_id = f"run_{uuid.uuid4().hex}"
        bus = self.bus(session.id)

        async def emit(event_type: str, data: dict[str, Any]) -> None:
            envelope = await bus.publish(event_type, data, turn_id=run_id)
            if event_type in {"message.end", "tool.end", "run.end", "plan", "checkpoint"}:
                self._watermarks[session.id] = envelope.seq

        task = asyncio.create_task(self._run(session, content, attachments or [], emit, run_id))
        self._tasks[session.id] = task
        task.add_done_callback(lambda finished: self._forget_task(session.id, finished))
        return run_id, bus.last_seq()

    def _forget_task(self, session_id: str, task: asyncio.Task[Any]) -> None:
        with self._operation_lock:
            if self._tasks.get(session_id) is task:
                self._tasks.pop(session_id, None)
        self._drain_queue(session_id)

    def _drain_queue(self, session_id: str) -> None:
        """Start the next queued prompt after the active task has finished."""

        with self._operation_lock:
            if self.is_running(session_id):
                return
            queue = self._queues.get(session_id)
            if not queue:
                return
            prompt = queue.popleft()
            if not queue:
                self._queues.pop(session_id, None)
            session = self.store.get_session(session_id)
            if session is None:
                return
            try:
                self.start_run(session, prompt.content, prompt.attachments)
            except Exception as exc:
                # A queued item that cannot start must surface as a terminal
                # error instead of silently disappearing or leaving the queue
                # visible forever.
                self._queues.pop(session_id, None)
                asyncio.create_task(self._queued_prompt_failed(session_id, str(exc)))

    async def _queued_prompt_failed(self, session_id: str, message: str) -> None:
        bus = self.bus(session_id)
        await bus.publish(
            "error",
            {"code": "queued_prompt_failed", "message": message, "retryable": False},
        )
        await bus.publish("run.end", {"reason": "error", "error": message})

    @contextlib.contextmanager
    def _exclusive_operation(self, session_id: str):
        """Reserve an idle workspace across awaits and worker-thread operations."""

        session = self.store.get_session(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")
        workspace = Path(session.cwd).resolve()
        with self._operation_lock:
            for active_id, task in self._tasks.items():
                active = self.store.get_session(active_id)
                if (
                    not task.done()
                    and active is not None
                    and _overlaps(workspace, Path(active.cwd).resolve())
                ):
                    raise SessionBusy("Workspace has an active run; stop it before this operation")
            if any(_overlaps(workspace, root) for root in self._workspace_operations):
                raise SessionBusy("Workspace is busy with another operation")
            self._workspace_operations.add(workspace)
        try:
            yield
        finally:
            with self._operation_lock:
                self._workspace_operations.discard(workspace)

    async def _run(
        self,
        session: Session,
        content: str,
        attachments: list[MessageAttachment],
        emit,
        run_id: str,
    ) -> None:
        cancelled = False

        async def resolve_permission(request: PermissionRequest, request_id: str) -> Approval:
            return await self._resolve_permission(request, request_id, session_id=session.id)

        try:
            await self.loop.run(
                session,
                content,
                emit=emit,
                permission_resolver=resolve_permission,
                model_override=session.model,
                extend_budget=True,
                attachments=attachments,
            )
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as exc:  # the loop already reports most failures
            await emit(
                "error",
                {"code": "internal_error", "message": str(exc), "retryable": False},
            )
            await emit("run.end", {"reason": "error", "error": str(exc)})
        finally:
            if not cancelled:
                other_active = any(
                    not task.done()
                    for session_id, task in self._tasks.items()
                    if session_id != session.id
                )
                if not other_active:
                    self.evolution_trigger.after_session(self.store, session.id)

    async def abort(self, session_id: str) -> bool:
        with self._operation_lock:
            self._queues.pop(session_id, None)
            task = self._tasks.get(session_id)
        if task is None or task.done():
            return False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        return True

    # ------------------------------------------------------------------
    # Permissions
    # ------------------------------------------------------------------
    async def _resolve_permission(
        self, request: PermissionRequest, request_id: str, *, session_id: str | None = None
    ) -> Approval:
        future: asyncio.Future[Approval] = asyncio.get_running_loop().create_future()
        self._waiters[request_id] = future
        if session_id is not None:
            self._waiter_sessions[request_id] = session_id
        try:
            return await asyncio.wait_for(future, timeout=PERMISSION_TIMEOUT_S)
        except TimeoutError:
            return Approval(decision="deny", reason="permission request timed out")
        finally:
            self._waiters.pop(request_id, None)
            self._waiter_sessions.pop(request_id, None)

    def resolve_permission(
        self,
        request_id: str,
        decision: str,
        reason: str | None = None,
        *,
        session_id: str | None = None,
    ) -> bool:
        if session_id is not None and self._waiter_sessions.get(request_id) != session_id:
            return False
        future = self._waiters.get(request_id)
        if future is None or future.done():
            return False
        if decision not in {"once", "session", "always", "deny"}:
            return False
        future.set_result(Approval(decision=decision, reason=reason))  # type: ignore[arg-type]
        return True

    # ------------------------------------------------------------------
    # Models and lifecycle
    # ------------------------------------------------------------------
    async def list_models(self, provider: str) -> list[ModelInfo]:
        return await self.registry.list_models(provider)

    def has_credential(self, provider: str) -> bool:
        config = self.config.providers.get(provider)
        if config is None:
            return False
        if config.auth.scheme == "none":
            return True
        if config.auth.env and os.environ.get(config.auth.env):
            return True
        return self.auth.configured(provider)

    async def reload_config(self, config: Config) -> None:
        """Apply a newly written config without restarting the daemon."""

        old_registry = self.registry
        self.config = config
        self.registry = ProviderRegistry(config, self.auth)
        self.router = RoleRouter(config, self.registry)
        self.loop.config = config
        self.loop.router = self.router
        await old_registry.aclose()

    # ------------------------------------------------------------------
    # TUI support
    # ------------------------------------------------------------------
    def messages(self, session_id: str) -> list[dict[str, Any]]:
        return self.store.get_message_views(session_id)

    async def mcp_status(self, *, refresh: bool = True) -> list[dict[str, Any]]:
        return await self.loop.mcp_status(refresh=refresh)

    async def restart_mcp(self, name: str) -> dict[str, Any]:
        return await self.loop.restart_mcp(name)

    def usage(self, session: Session) -> dict[str, Any]:
        totals = self.store.usage_totals(session.id)
        context_window = self._context_window(session.model)
        last_input, _last_cache = self.store.last_usage_tokens(session.id)
        # OpenAI-style prompt/input tokens already include the cache-read subset.
        context_tokens = last_input or None
        context_percent = None
        if context_window and context_tokens is not None:
            context_percent = round(min(100.0, (context_tokens / context_window) * 100.0), 2)
        return {
            **totals.model_dump(mode="json"),
            "context_window": context_window,
            "context_tokens": context_tokens,
            "context_percent": context_percent,
        }

    def _context_window(self, model: str | None) -> int | None:
        if not model:
            return None
        provider, _, model_id = model.partition("/")
        if not provider or not model_id:
            return None
        try:
            return load_profile(provider, model_id).context_window
        except (OSError, ValueError):
            return None

    def update_session(
        self,
        session_id: str,
        *,
        model: str | None = None,
        mode: str | None = None,
        title: str | None = None,
        effort: str | None = None,
    ) -> Session | None:
        if effort is not None:
            session = self.store.get_session(session_id)
            if session is None:
                return None
            metadata = dict(session.metadata)
            if effort == "off":
                metadata.pop("effort", None)
            else:
                metadata["effort"] = effort
            self.store.update_session(session_id, metadata=metadata)
        self.store.update_session(session_id, model=model, mode=mode, title=title)
        return self.store.get_session(session_id)

    def permission_mode(self) -> str:
        return self.config.permissions.mode

    def set_permission_mode(self, mode: str) -> str:
        if mode not in {"ask", "auto-edit", "yolo"}:
            raise ValueError(f"invalid permission mode: {mode}")
        self.config = self.config.model_copy(
            update={"permissions": self.config.permissions.model_copy(update={"mode": mode})}
        )
        self.loop.config = self.config
        return mode

    def fork_session(
        self,
        session_id: str,
        title: str | None = None,
        checkpoint_id: str | None = None,
    ) -> Session:
        if checkpoint_id:
            with self._exclusive_operation(session_id):
                return self._fork_session(session_id, title, checkpoint_id)
        return self._fork_session(session_id, title, checkpoint_id)

    def _fork_session(
        self, session_id: str, title: str | None, checkpoint_id: str | None
    ) -> Session:
        checkpoint = None
        if checkpoint_id:
            # Validate before creating anything so a rejected fork has no
            # side effects.
            checkpoint = self.store.get_checkpoint(checkpoint_id)
            if checkpoint is None or checkpoint["session_id"] != session_id:
                raise ValueError(f"Checkpoint not found: {checkpoint_id}")
        forked = self.store.fork_session(session_id, title=title)
        if checkpoint is not None:
            manager = CheckpointManager(Path(forked.cwd), session_id, store=self.store)
            try:
                manager.restore(checkpoint["commit_hash"])
            except CheckpointError as exc:
                self.store.delete_session(forked.id)
                raise ValueError(str(exc)) from exc
            self.store.append_trajectory(
                forked.id,
                {
                    "type": "rewind",
                    "checkpoint_id": checkpoint_id,
                    "commit": checkpoint["commit_hash"],
                    "source_session": session_id,
                },
            )
        return forked

    # ------------------------------------------------------------------
    # Checkpoints and context
    # ------------------------------------------------------------------
    def checkpoints(self, session_id: str) -> list[dict[str, Any]]:
        return self.store.list_checkpoints(session_id)

    def create_checkpoint(self, session_id: str, label: str | None = None) -> dict[str, Any]:
        with self._exclusive_operation(session_id):
            return self._create_checkpoint(session_id, label)

    def _create_checkpoint(self, session_id: str, label: str | None) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")
        manager = CheckpointManager(Path(session.cwd), session.id, store=self.store)
        checkpoint_id = manager.snapshot(label or "manual checkpoint", allow_empty=True)
        if checkpoint_id is None:
            raise ValueError("Checkpoint could not be created")
        checkpoint = self.store.get_checkpoint(checkpoint_id)
        if checkpoint is None:
            raise ValueError("Checkpoint metadata is missing")
        return checkpoint

    def rewind(
        self,
        session_id: str,
        *,
        checkpoint_id: str | None = None,
        steps: int = 1,
        confirm: bool = False,
    ) -> dict[str, Any]:
        with self._exclusive_operation(session_id):
            return self._rewind(
                session_id, checkpoint_id=checkpoint_id, steps=steps, confirm=confirm
            )

    def _rewind(
        self, session_id: str, *, checkpoint_id: str | None, steps: int, confirm: bool
    ) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")
        checkpoints = self.store.list_checkpoints(session_id)
        if checkpoint_id:
            checkpoint = self.store.get_checkpoint(checkpoint_id)
            if checkpoint is None or checkpoint["session_id"] != session_id:
                raise ValueError(f"Checkpoint not found: {checkpoint_id}")
        else:
            index = len(checkpoints) - 1 - max(steps, 1)
            if index < 0:
                raise ValueError("No previous checkpoint is available")
            checkpoint = checkpoints[index]
        if not confirm:
            raise ConfirmationRequired(checkpoint)
        manager = CheckpointManager(Path(session.cwd), session.id, store=self.store)
        try:
            manager.restore(checkpoint["commit_hash"])
        except CheckpointError as exc:
            raise ValueError(str(exc)) from exc
        self.store.append_trajectory(
            session_id,
            {
                "type": "rewind",
                "checkpoint_id": checkpoint["id"],
                "commit": checkpoint["commit_hash"],
            },
        )
        return checkpoint

    async def compact_session(self, session_id: str) -> dict[str, Any]:
        with self._exclusive_operation(session_id):
            return await self._compact_session(session_id)

    async def _compact_session(self, session_id: str) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")
        through = self.store.max_message_seq(session_id)
        previous = str(session.metadata.get("context_summary", ""))
        messages = self.store.get_context_messages(session_id)
        if previous and messages and messages[0].role == "system":
            messages = messages[1:]
        todo_text = "\n".join(
            f"[{item.get('status', 'pending')}] {item.get('content', '')}"
            for item in (session.metadata or {}).get("todos", [])
            if isinstance(item, dict)
        )
        before = sum(estimate_tokens(message.content or "") for message in messages)
        if previous:
            before += estimate_tokens(previous)
        summary = fallback_summary(messages, todo_text)
        if previous:
            summary = previous + "\n\n" + summary
        usage = Usage()
        if self.config.roles.fast:
            try:

                async def emit(_event) -> None:
                    return None

                outcome = await self.router.stream(
                    "fast",
                    messages=compaction_prompt(messages, todo_text, previous_summary=previous),
                    tools=[],
                    session_id=session_id,
                    emit=emit,
                    temperature=0.0,
                    max_tokens=1200,
                )
                usage = outcome.usage
                if outcome.text and outcome.text.strip():
                    summary = outcome.text.strip()
            except (ConfigError, ProviderError):
                # A failed fast-role call falls back to the deterministic summary.
                pass
        summary = summary[: self.config.context.summary_max_chars]
        self.store.set_context_summary(session_id, summary, through)
        if usage.input_tokens or usage.output_tokens or usage.allowance:
            self.store.record_usage(
                session_id=session_id,
                step_id="compact",
                provider=None,
                model=session.model,
                usage=usage,
            )
        after = estimate_tokens(summary)
        return {
            "summary": summary,
            "through_seq": through,
            "before_tokens": before,
            "after_tokens": after,
        }

    def plan(self, session_id: str) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")
        path = Path(session.cwd) / ".siwah" / "plan.md"
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            content = ""
        return {"path": str(path), "content": content, "exists": bool(content)}

    def tool_output(self, session_id: str, tool_call_id: str) -> dict[str, Any]:
        record = self.store.get_tool_call(tool_call_id)
        if record is None or record["session_id"] != session_id:
            raise ValueError(f"Tool call not found: {tool_call_id}")
        result = record.get("result") or {}
        data = result.get("data") if isinstance(result, dict) else {}
        if isinstance(data, dict) and isinstance(data.get("full_output"), str):
            output = data["full_output"]
        elif isinstance(result, dict):
            output = str(result.get("output", ""))
        else:
            output = ""
        return {
            "tool_call_id": tool_call_id,
            "name": record["name"],
            "output": output,
            "handle": data.get("handle") if isinstance(data, dict) else None,
        }

    def skills(self, workspace: Path | None = None) -> list[Any]:
        return list_skills(workspace)

    def skill(self, name: str, workspace: Path | None = None) -> Any:
        return read_skill(name, workspace)

    def memory(self, cwd: str | None = None) -> Any:
        workspace = Path(cwd or Path.cwd()).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError(f"Workspace does not exist: {workspace}")
        return read_memory(workspace)

    def init_memory(self, cwd: str) -> Any:
        workspace = Path(cwd).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError(f"Workspace does not exist: {workspace}")
        init_project_memory(workspace)
        return init_agents_file(workspace)

    def write_quick_note(self, content: str) -> Any:
        return quick_note(content)

    async def files(self, session_id: str, limit: int = 500) -> list[str]:
        session = self.store.get_session(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")
        return await self.files_for_cwd(session.cwd, limit)

    async def files_for_cwd(self, cwd: str, limit: int = 500) -> list[str]:
        return await asyncio.to_thread(_list_files, Path(cwd), limit)

    def set_default_effort(self, effort: str | None) -> str | None:
        normalized = effort.strip().lower() if effort else None
        if normalized in {"", "default", "none", "off"}:
            normalized = None
        try:
            update_agent_effort(normalized)
        except (OSError, ValueError) as exc:
            raise ValueError(f"Could not save effort: {exc}") from exc
        self.config = self.config.model_copy(
            update={"agent": self.config.agent.model_copy(update={"effort": normalized})}
        )
        self.loop.config = self.config
        return normalized

    def auth_status(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for name, provider in self.config.providers.items():
            env_name = provider.auth.env
            result.append(
                {
                    "provider": name,
                    "scheme": provider.auth.scheme,
                    "env": env_name,
                    "env_set": bool(env_name and os.environ.get(env_name)),
                    "file_set": self.auth.configured(name),
                }
            )
        return result

    def auth_login(self, provider: str, api_key: str) -> None:
        if provider not in self.config.providers:
            raise ValueError(f"Unknown provider: {provider}")
        self.auth.save_key(provider, api_key)

    async def run_shell(
        self, session_id: str, command: str, *, approved: bool = False, timeout_s: float = 120.0
    ) -> dict[str, Any]:
        with self._exclusive_operation(session_id):
            return await self._run_shell(
                session_id, command, approved=approved, timeout_s=timeout_s
            )

    async def _run_shell(
        self, session_id: str, command: str, *, approved: bool, timeout_s: float
    ) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")
        workspace = Path(session.cwd).resolve()
        policy = PermissionPolicy(workspace, self.config.permissions)
        request = PermissionRequest(
            tool="bash",
            kind="bash",
            command=command,
            preview=f"$ {command}",
        )
        decision = policy.check(request)
        if decision == "deny":
            raise ShellDenied("Command denied by policy")
        if decision == "ask" and not approved:
            raise ShellPermissionRequired(request.preview)

        async def approver(_request: PermissionRequest) -> Approval:
            return Approval(decision="once")

        context = ToolContext(
            workspace=workspace,
            policy=policy,
            session_id=session_id,
            agent_config=self.config.agent,
            approver=approver,
            sandbox=Sandbox(self.config.sandbox, cwd=workspace)
            if self.config.sandbox.enabled
            else None,
        )
        result = await BashTool().run(context, command=command, timeout_s=timeout_s)
        return {
            "ok": result.ok,
            "output": result.output,
            "exit_code": (result.data or {}).get("exit_code") if result.ok else None,
        }

    async def close(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        for task in list(self._tasks.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self.loop.close_jobs()
        await self.registry.aclose()
        self.store.close()


class SessionBusy(RuntimeError):
    """A workspace mutation conflicts with a run or another reserved operation."""


def _overlaps(left: Path, right: Path) -> bool:
    return left.is_relative_to(right) or right.is_relative_to(left)


def _list_files(root: Path, limit: int) -> list[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "-c", "core.quotepath=false", "ls-files", "-z"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if completed.returncode == 0:
            decoded = completed.stdout.decode("utf-8", errors="replace")
            files = [name for name in decoded.split("\0") if name]
            if files:
                return files[:limit]
    except (OSError, subprocess.SubprocessError):
        pass
    files: list[str] = []
    skip = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__"}
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in skip]
        for name in filenames:
            path = Path(directory) / name
            files.append(str(path.relative_to(root)).replace("\\", "/"))
            if len(files) >= limit:
                return files
    return files


__all__ = [
    "ConfigError",
    "DaemonRuntime",
    "ShellDenied",
    "ShellPermissionRequired",
]
