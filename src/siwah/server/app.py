"""FastAPI application: HTTP + SSE protocol for local clients."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import secrets
import subprocess
import time
import webbrowser
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from ..adapt.promoter import HarnessRepository
from ..adapt.state import is_paused, load_state, set_paused
from ..auth import AuthError
from ..commands import list_commands_for
from ..config import Config, ConfigError, add_default_providers, load_config
from ..core.storage import Session
from ..core.types import MessageAttachment as CoreMessageAttachment
from ..core.types import ModelInfo
from ..kernel.audit import audit_event, read_events
from ..paths import harness_dir
from ..providers.base import ProviderError
from ..providers.chatgpt_auth import ChatGPTSignInPending
from ..providers.openai_responses import supports_chatgpt_login
from ..providers.openrouter_auth import OpenRouterLogin, supports_browser_login
from ..update import apply_update, check_for_update
from ..version import __version__
from .auth import require_token
from .events import sse_frame
from .runtime import (
    ConfirmationRequired,
    DaemonRuntime,
    SessionBusy,
    ShellDenied,
    ShellPermissionRequired,
)
from .schema import (
    AuthBrowserFinishRequest,
    AuthBrowserStartRequest,
    AuthBrowserStartResponse,
    AuthLoginRequest,
    AuthProviderStatus,
    AuthStatusResponse,
    Capabilities,
    ChangesResponse,
    ChatGPTAccount,
    ChatGPTAccountsResponse,
    ChatGPTSignOutResponse,
    CheckpointCreateRequest,
    CheckpointListResponse,
    CheckpointSummary,
    CommandInfo,
    CommandListResponse,
    CompactResponse,
    CreateSessionRequest,
    EffortRequest,
    EffortResponse,
    EventSchemaResponse,
    EvolutionPauseRequest,
    EvolutionRollbackRequest,
    EvolutionRollbackResponse,
    EvolutionSummary,
    FileListResponse,
    ForkSessionRequest,
    ForkSessionResponse,
    InitMemoryRequest,
    InitMemoryResponse,
    LearningGraphResponse,
    LearningLesson,
    LearningSkill,
    McpServerStatus,
    McpStatusResponse,
    MemoryFileInfo,
    MemoryResponse,
    MessageListResponse,
    MessageView,
    MetaResponse,
    ModelCatalogResponse,
    ModelSummary,
    OkResponse,
    PermissionDecisionRequest,
    PermissionDecisionResponse,
    PermissionModeRequest,
    PermissionModeResponse,
    PlanResponse,
    ProviderModels,
    QuestionReply,
    QuestionRequest,
    QueuedPromptResponse,
    QuickNoteRequest,
    QuickNoteResponse,
    RewindRequest,
    RewindResponse,
    RunTimingResponse,
    SendMessageRequest,
    SendMessageResponse,
    SessionListResponse,
    SessionResponse,
    ShellRequest,
    ShellResponse,
    SkillContentResponse,
    SkillInfo,
    SkillListResponse,
    ToolOutputResponse,
    UpdateApplyResponse,
    UpdateSessionRequest,
    UpdateStatusResponse,
    UsageResponse,
)

PROTOCOL_VERSION = "0.1"


def create_app(
    config: Config | None = None,
    *,
    runtime: DaemonRuntime | None = None,
    token: str | None = None,
    nonce: str = "",
) -> FastAPI:
    config = config or (runtime.config if runtime else load_config())
    browser_logins: dict[str, tuple[str, OpenRouterLogin]] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        if runtime is not None:
            runtime.registry.chatgpt_auth.close()
            await runtime.close()

    app = FastAPI(
        title="siwah",
        version=__version__,
        description="Local HTTP + SSE protocol for siwah clients.",
        openapi_url="/v1/openapi.json",
        docs_url=None,
        redoc_url=None,
        openapi_tags=[{"name": "siwah", "description": "Local protocol"}],
        lifespan=lifespan,
    )
    dependency = Depends(require_token(token))
    app.state.runtime = runtime

    @app.middleware("http")
    async def validate_protocol(request: Request, call_next):
        supplied = request.headers.get("x-siwah-protocol")
        if supplied is not None and supplied != PROTOCOL_VERSION:
            return JSONResponse(
                status_code=409,
                content=error_payload(
                    "protocol_mismatch",
                    f"Unsupported protocol {supplied!r}; this daemon requires {PROTOCOL_VERSION}",
                ),
            )
        return await call_next(request)

    @app.exception_handler(SessionBusy)
    async def session_busy_handler(_request, exc: SessionBusy):
        return JSONResponse(
            status_code=409, content=error_payload("session_busy", str(exc), retryable=True)
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request, exc: HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            body = {"error": detail}
        else:
            body = {"error": {"code": "http_error", "message": str(detail)}}
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(ValueError)
    async def value_error_handler(_request, exc: ValueError):
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "bad_request", "message": str(exc)}},
        )

    def require_runtime() -> DaemonRuntime:
        if runtime is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "runtime_unavailable", "message": "Daemon runtime is not ready"},
            )
        return runtime

    def session_response(session: Session, *, include_queue: bool = False) -> SessionResponse:
        data = session.model_dump()
        if runtime is not None:
            running = runtime.is_running(session.id)
            # A daemon restart loses in-memory tasks but persisted session rows
            # may still say "running". Never let that stale status block the UI.
            if data.get("status") == "running" and not running:
                data["status"] = "idle"
            elif running:
                data["status"] = "running"
            data["last_event_seq"] = runtime.last_event_seq(session.id)
            if include_queue:
                data["prompt_queue"] = runtime.queue_snapshot(session.id)
                tool_activity = runtime.bus(session.id).tool_activity()
                if tool_activity:
                    data["metadata"] = {
                        **(data.get("metadata") or {}),
                        "tool_activity": tool_activity,
                    }
            if running:
                if runtime.is_waiting_permission(session.id):
                    data["metadata"] = {**(data.get("metadata") or {}), "waiting_permission": True}
                active_run_id = runtime.active_run_id(session.id)
                if active_run_id:
                    data["metadata"] = {
                        **(data.get("metadata") or {}),
                        "active_run_id": active_run_id,
                    }
                phase = runtime.bus(session.id).run_phase()
                if phase:
                    data["metadata"] = {**(data.get("metadata") or {}), "run_phase": phase}
        else:
            data["last_event_seq"] = 0
        return SessionResponse.model_validate(data)

    def checkpoint_summary(record: dict[str, Any]) -> CheckpointSummary:
        return CheckpointSummary(
            id=str(record["id"]),
            label=record.get("label"),
            commit=record.get("commit_hash"),
            created_at=str(record["created_at"]),
        )

    @app.get("/v1/meta", response_model=MetaResponse, dependencies=[dependency], tags=["siwah"])
    async def meta() -> MetaResponse:
        return MetaResponse(
            version=__version__,
            protocol=PROTOCOL_VERSION,
            daemon_started_at=runtime.started_at if runtime else "1970-01-01T00:00:00Z",
            default_effort=runtime.config.agent.effort if runtime else None,
            capabilities=Capabilities(
                sse_resume=True,
                permission_modes=["ask", "auto-edit", "yolo"],
                features=[
                    "sessions",
                    "permissions",
                    "interactive_questions",
                    "openapi",
                    "files",
                    "fork",
                    "shell",
                    "auth",
                    "providers",
                    "skills",
                    "memory",
                    "checkpoints",
                    "rewind",
                    "compact",
                    "plan",
                    "verify",
                    "repomap",
                    "update",
                ],
            ),
        )

    @app.post(
        "/v1/agent/effort", response_model=EffortResponse, dependencies=[dependency], tags=["siwah"]
    )
    async def set_agent_effort(body: EffortRequest) -> EffortResponse:
        daemon = require_runtime()
        try:
            value = daemon.set_default_effort(body.effort)
        except ValueError as exc:
            raise HTTPException(400, detail={"code": "effort_error", "message": str(exc)}) from exc
        return EffortResponse(effort=value)

    @app.get("/v1/health", response_model=OkResponse, dependencies=[dependency], tags=["siwah"])
    async def health(request: Request, response: Response) -> OkResponse:
        if nonce:
            challenge = request.headers.get("x-siwah-challenge", "")
            if challenge:
                response.headers["X-Siwah-Proof"] = hmac.new(
                    nonce.encode(), challenge.encode(), hashlib.sha256
                ).hexdigest()
        return OkResponse()

    @app.get(
        "/v1/update", response_model=UpdateStatusResponse, dependencies=[dependency], tags=["siwah"]
    )
    async def update_status(refresh: bool = Query(False)) -> UpdateStatusResponse:
        status = await asyncio.to_thread(check_for_update, refresh=refresh)
        return UpdateStatusResponse(**asdict(status))

    @app.post(
        "/v1/update", response_model=UpdateApplyResponse, dependencies=[dependency], tags=["siwah"]
    )
    async def update_apply() -> UpdateApplyResponse:
        result = await asyncio.to_thread(apply_update)
        if not result.ok:
            raise HTTPException(
                status_code=409,
                detail={"code": "update_failed", "message": result.detail},
            )
        return UpdateApplyResponse(**asdict(result))

    @app.get(
        "/v1/sessions",
        response_model=SessionListResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def list_sessions() -> SessionListResponse:
        daemon = require_runtime()
        return SessionListResponse(
            sessions=[session_response(session) for session in daemon.list_sessions()]
        )

    @app.post(
        "/v1/sessions",
        response_model=SessionResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def create_session(body: CreateSessionRequest) -> SessionResponse:
        daemon = require_runtime()
        session = daemon.create_session(
            cwd=body.cwd,
            model=body.model,
            mode=body.mode,
            title=body.title,
        )
        return session_response(session, include_queue=True)

    @app.get(
        "/v1/sessions/{session_id}",
        response_model=SessionResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def get_session(session_id: str) -> SessionResponse:
        daemon = require_runtime()
        session = daemon.get_session(session_id)
        if session is None:
            raise HTTPException(404, detail={"code": "session_not_found", "message": session_id})
        return session_response(session, include_queue=True)

    @app.get(
        "/v1/sessions/{session_id}/messages",
        response_model=MessageListResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def messages(session_id: str) -> MessageListResponse:
        daemon = require_runtime()
        if daemon.get_session(session_id) is None:
            raise HTTPException(404, detail={"code": "session_not_found", "message": session_id})
        return MessageListResponse(
            messages=[MessageView.model_validate(item) for item in daemon.messages(session_id)]
        )

    @app.get(
        "/v1/sessions/{session_id}/usage",
        response_model=UsageResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def usage(session_id: str) -> UsageResponse:
        daemon = require_runtime()
        session = daemon.get_session(session_id)
        if session is None:
            raise HTTPException(404, detail={"code": "session_not_found", "message": session_id})
        return UsageResponse.model_validate(daemon.usage(session))

    @app.get(
        "/v1/mcp",
        response_model=McpStatusResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def mcp_status(refresh: bool = Query(default=True)) -> McpStatusResponse:
        daemon = require_runtime()
        return McpStatusResponse(
            servers=[
                McpServerStatus.model_validate(item)
                for item in await daemon.mcp_status(refresh=refresh)
            ]
        )

    @app.post(
        "/v1/mcp/{name}/restart",
        response_model=McpServerStatus,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def restart_mcp(name: str) -> McpServerStatus:
        daemon = require_runtime()
        try:
            return McpServerStatus.model_validate(await daemon.restart_mcp(name))
        except ValueError as exc:
            raise HTTPException(404, detail={"code": "mcp_not_found", "message": str(exc)}) from exc

    @app.patch(
        "/v1/sessions/{session_id}",
        response_model=SessionResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def update_session(session_id: str, body: UpdateSessionRequest) -> SessionResponse:
        daemon = require_runtime()
        if daemon.get_session(session_id) is None:
            raise HTTPException(404, detail={"code": "session_not_found", "message": session_id})
        updated = daemon.update_session(
            session_id,
            model=body.model,
            mode=body.mode,
            title=body.title,
            effort=body.effort,
        )
        if updated is None:
            raise HTTPException(404, detail={"code": "session_not_found", "message": session_id})
        return session_response(updated)

    @app.post(
        "/v1/sessions/{session_id}/fork",
        response_model=ForkSessionResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def fork_session(session_id: str, body: ForkSessionRequest) -> ForkSessionResponse:
        daemon = require_runtime()
        if daemon.get_session(session_id) is None:
            raise HTTPException(404, detail={"code": "session_not_found", "message": session_id})
        try:
            forked = await asyncio.to_thread(
                daemon.fork_session, session_id, body.title, body.checkpoint_id
            )
        except ValueError as exc:
            raise HTTPException(
                400, detail={"code": "checkpoint_invalid", "message": str(exc)}
            ) from exc
        return ForkSessionResponse(session=session_response(forked))

    @app.get(
        "/v1/files",
        response_model=FileListResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def files(
        session_id: str | None = Query(default=None),
        cwd: str | None = Query(default=None),
        limit: int = Query(default=500, ge=1, le=5000),
    ) -> FileListResponse:
        daemon = require_runtime()
        if session_id:
            if daemon.get_session(session_id) is None:
                raise HTTPException(
                    404, detail={"code": "session_not_found", "message": session_id}
                )
            return FileListResponse(files=await daemon.files(session_id, limit=limit))
        if not cwd:
            raise HTTPException(
                400,
                detail={
                    "code": "missing_workspace",
                    "message": "Pass session_id or cwd",
                },
            )
        root = Path(cwd).expanduser()
        if not root.is_dir():
            raise HTTPException(400, detail={"code": "cwd_not_found", "message": str(root)})
        return FileListResponse(files=await daemon.files_for_cwd(str(root), limit=limit))

    @app.get(
        "/v1/harness/skills",
        response_model=SkillListResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def harness_skills(cwd: str | None = Query(default=None)) -> SkillListResponse:
        daemon = require_runtime()
        workspace = Path(cwd).expanduser() if cwd else None
        return SkillListResponse(
            skills=[
                SkillInfo(name=skill.name, description=skill.description)
                for skill in daemon.skills(workspace)
            ]
        )

    @app.get(
        "/v1/harness/skills/{name}",
        response_model=SkillContentResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def harness_skill(
        name: str, cwd: str | None = Query(default=None)
    ) -> SkillContentResponse:
        daemon = require_runtime()
        workspace = Path(cwd).expanduser() if cwd else None
        try:
            skill = daemon.skill(name, workspace)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(
                404, detail={"code": "skill_not_found", "message": str(exc)}
            ) from exc
        return SkillContentResponse(
            name=skill.name,
            description=skill.description,
            content=skill.content,
            path=str(skill.path),
        )

    @app.get(
        "/v1/harness/memory",
        response_model=MemoryResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def harness_memory(cwd: str | None = Query(default=None)) -> MemoryResponse:
        daemon = require_runtime()
        try:
            snapshot = daemon.memory(cwd)
        except ValueError as exc:
            raise HTTPException(400, detail={"code": "cwd_not_found", "message": str(exc)}) from exc

        def file_info(item: Any) -> MemoryFileInfo:
            return MemoryFileInfo(path=str(item.path), content=item.content, exists=item.exists)

        return MemoryResponse(
            user=file_info(snapshot.user),
            project=file_info(snapshot.project),
            quick=file_info(snapshot.quick),
        )

    @app.get(
        "/v1/harness/learning",
        response_model=LearningGraphResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def harness_learning(cwd: str | None = Query(default=None)) -> LearningGraphResponse:
        daemon = require_runtime()
        try:
            graph = daemon.learning(cwd)
        except ValueError as exc:
            raise HTTPException(400, detail={"code": "cwd_not_found", "message": str(exc)}) from exc
        return LearningGraphResponse(
            skills=[LearningSkill(**item) for item in graph["skills"]],
            lessons=[LearningLesson(**item) for item in graph["lessons"]],
            evolution_tag=graph["evolution_tag"],
            evolution_status=graph["evolution_status"],
        )

    @app.post(
        "/v1/harness/init",
        response_model=InitMemoryResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def harness_init(body: InitMemoryRequest) -> InitMemoryResponse:
        daemon = require_runtime()
        try:
            path, created, content = daemon.init_memory(body.cwd)
        except ValueError as exc:
            raise HTTPException(400, detail={"code": "cwd_not_found", "message": str(exc)}) from exc
        return InitMemoryResponse(path=str(path), created=created, content=content)

    @app.post(
        "/v1/harness/note",
        response_model=QuickNoteResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def harness_note(body: QuickNoteRequest) -> QuickNoteResponse:
        daemon = require_runtime()
        try:
            path, changed = daemon.write_quick_note(body.content)
        except ValueError as exc:
            raise HTTPException(400, detail={"code": "note_rejected", "message": str(exc)}) from exc
        return QuickNoteResponse(path=str(path), changed=changed)

    @app.get(
        "/v1/auth/status",
        response_model=AuthStatusResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def auth_status() -> AuthStatusResponse:
        daemon = require_runtime()
        return AuthStatusResponse(
            providers=[AuthProviderStatus.model_validate(item) for item in daemon.auth_status()]
        )

    @app.post(
        "/v1/providers/defaults",
        response_model=AuthStatusResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def provider_defaults() -> AuthStatusResponse:
        daemon = require_runtime()
        try:
            new_config = add_default_providers()
        except (OSError, ConfigError) as exc:
            raise HTTPException(
                500,
                detail={"code": "config_write_failed", "message": str(exc)},
            ) from exc
        providers = {**new_config.providers, **daemon.config.providers}
        if providers != daemon.config.providers:
            await daemon.reload_config(daemon.config.model_copy(update={"providers": providers}))
        return AuthStatusResponse(
            providers=[AuthProviderStatus.model_validate(item) for item in daemon.auth_status()]
        )

    @app.post(
        "/v1/auth/login",
        response_model=OkResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def auth_login(body: AuthLoginRequest) -> OkResponse:
        daemon = require_runtime()
        try:
            daemon.auth_login(body.provider, body.api_key)
        except ValueError as exc:
            raise HTTPException(400, detail={"code": "auth_error", "message": str(exc)}) from exc
        return OkResponse()

    @app.post(
        "/v1/auth/browser/{provider}",
        response_model=AuthBrowserStartResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def start_browser_login(
        provider: str, body: AuthBrowserStartRequest | None = None
    ) -> AuthBrowserStartResponse:
        daemon = require_runtime()
        preset = daemon.config.providers.get(provider)
        if preset is not None and supports_chatgpt_login(preset):
            try:
                flow = await daemon.registry.chatgpt_auth.begin(body.client_id if body else None)
            except AuthError as exc:
                raise HTTPException(
                    400, detail={"code": "auth_error", "message": str(exc)}
                ) from exc
            with contextlib.suppress(OSError, webbrowser.Error):
                await asyncio.to_thread(webbrowser.open, flow.url)
            return AuthBrowserStartResponse(flow_id=flow.id, url=flow.url)
        if preset is None or not supports_browser_login(preset):
            raise HTTPException(
                400, detail={"code": "auth_error", "message": "Browser sign-in unavailable."}
            )
        for flow_id, (_, pending) in list(browser_logins.items()):
            if pending.expires_at <= time.monotonic():
                browser_logins.pop(flow_id)
        if len(browser_logins) >= 10:
            raise HTTPException(
                429, detail={"code": "auth_error", "message": "Too many pending sign-ins."}
            )
        login = OpenRouterLogin()
        flow_id = secrets.token_urlsafe(24)
        browser_logins[flow_id] = (provider, login)
        url = login.authorization_url()
        with contextlib.suppress(OSError, webbrowser.Error):
            await asyncio.to_thread(webbrowser.open, url)
        return AuthBrowserStartResponse(flow_id=flow_id, url=url)

    @app.post(
        "/v1/auth/browser/{provider}/complete",
        response_model=OkResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def finish_browser_login(provider: str, body: AuthBrowserFinishRequest) -> OkResponse:
        daemon = require_runtime()
        preset = daemon.config.providers.get(provider)
        if preset is not None and supports_chatgpt_login(preset):
            try:
                await daemon.registry.chatgpt_auth.finish(body.flow_id)
            except ChatGPTSignInPending as exc:
                raise HTTPException(
                    409, detail={"code": "auth_pending", "message": str(exc)}
                ) from exc
            except AuthError as exc:
                raise HTTPException(
                    400, detail={"code": "auth_error", "message": str(exc)}
                ) from exc
            daemon.registry.invalidate_chatgpt_catalog(provider)
            return OkResponse()
        pending = browser_logins.get(body.flow_id)
        if (
            preset is None
            or not supports_browser_login(preset)
            or pending is None
            or pending[0] != provider
            or not body.code
        ):
            raise HTTPException(
                400, detail={"code": "auth_error", "message": "Start a new browser sign-in."}
            )
        browser_logins.pop(body.flow_id)
        try:
            key = await pending[1].exchange(body.code)
            daemon.auth_login(provider, key)
        except (AuthError, ValueError) as exc:
            raise HTTPException(400, detail={"code": "auth_error", "message": str(exc)}) from exc
        return OkResponse()

    @app.get(
        "/v1/auth/chatgpt/accounts",
        response_model=ChatGPTAccountsResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def chatgpt_accounts() -> ChatGPTAccountsResponse:
        try:
            accounts = require_runtime().registry.chatgpt_auth.accounts()
        except AuthError as exc:
            raise HTTPException(
                400, detail={"code": "auth_store_error", "message": str(exc)}
            ) from exc
        return ChatGPTAccountsResponse(
            accounts=[
                ChatGPTAccount.model_validate(account, from_attributes=True) for account in accounts
            ]
        )

    @app.post(
        "/v1/auth/chatgpt/sign-out",
        response_model=ChatGPTSignOutResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def chatgpt_sign_out(body: AuthBrowserStartRequest) -> ChatGPTSignOutResponse:
        daemon = require_runtime()
        try:
            revoked = await daemon.registry.chatgpt_auth.sign_out(body.client_id)
        except AuthError as exc:
            raise HTTPException(400, detail={"code": "auth_error", "message": str(exc)}) from exc
        for name in daemon.config.providers:
            daemon.registry.invalidate_chatgpt_catalog(name)
        return ChatGPTSignOutResponse(revoked=revoked)

    @app.post(
        "/v1/sessions/{session_id}/shell",
        response_model=ShellResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def shell(session_id: str, body: ShellRequest) -> ShellResponse:
        daemon = require_runtime()
        if daemon.get_session(session_id) is None:
            raise HTTPException(404, detail={"code": "session_not_found", "message": session_id})
        try:
            result = await daemon.run_shell(
                session_id,
                body.command,
                approved=body.approved,
                timeout_s=body.timeout_s,
            )
        except ShellPermissionRequired as exc:
            raise HTTPException(
                409,
                detail={
                    "code": "permission_required",
                    "message": "Shell command needs approval",
                    "preview": exc.preview,
                },
            ) from exc
        except ShellDenied as exc:
            raise HTTPException(
                403, detail={"code": "permission_denied", "message": str(exc)}
            ) from exc
        return ShellResponse.model_validate(result)

    @app.get(
        "/v1/sessions/{session_id}/timing",
        response_model=RunTimingResponse | None,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def run_timing(session_id: str) -> RunTimingResponse | None:
        daemon = require_runtime()
        if daemon.get_session(session_id) is None:
            raise HTTPException(404, detail="Session not found")
        data = daemon.store.latest_run_timing(session_id)
        return RunTimingResponse.model_validate(data) if data else None

    @app.post(
        "/v1/sessions/{session_id}/messages",
        response_model=SendMessageResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def send_message(session_id: str, body: SendMessageRequest) -> SendMessageResponse:
        daemon = require_runtime()
        session = daemon.get_session(session_id)
        if session is None:
            raise HTTPException(404, detail={"code": "session_not_found", "message": session_id})
        if session.model is None and daemon.config.roles.main is None:
            raise HTTPException(
                400,
                detail={
                    "code": "model_not_configured",
                    "message": "Set [roles].main or pass a model when creating the session",
                },
            )
        try:
            if body.steer_run_id is not None:
                message_id, event_id = daemon.steer(session_id, body.content, body.steer_run_id)
                return SendMessageResponse(
                    session_id=session_id,
                    run_id=body.steer_run_id,
                    event_id=event_id,
                    status="steering",
                    message_id=message_id,
                )
            run_id, event_id, status = daemon.submit_run(
                session,
                body.content,
                [
                    CoreMessageAttachment(mime=item.mime, data=item.data, name=item.name)
                    for item in body.attachments
                ],
            )
        except RuntimeError as exc:
            raise HTTPException(409, detail={"code": "run_conflict", "message": str(exc)}) from exc
        if status == "queued":
            event_id = await daemon.publish_queue(session_id)
        return SendMessageResponse(
            session_id=session_id, run_id=run_id, event_id=event_id, status=status
        )

    @app.delete(
        "/v1/sessions/{session_id}/queue/{prompt_id}",
        response_model=QueuedPromptResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def remove_queued_prompt(session_id: str, prompt_id: str) -> QueuedPromptResponse:
        item = await require_runtime().remove_queued_prompt(session_id, prompt_id)
        if item is None:
            raise HTTPException(
                404,
                detail={
                    "code": "queued_prompt_not_found",
                    "message": "This prompt is no longer queued",
                },
            )
        return QueuedPromptResponse.model_validate(item, from_attributes=True)

    @app.delete(
        "/v1/sessions/{session_id}/corrections/{message_id}",
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def resolve_correction(session_id: str, message_id: str) -> dict[str, bool]:
        daemon = require_runtime()
        if daemon.get_session(session_id) is None:
            raise HTTPException(404, detail={"code": "session_not_found", "message": session_id})
        if not daemon.resolve_correction(session_id, message_id):
            raise HTTPException(
                404,
                detail={
                    "code": "correction_not_found",
                    "message": "This correction is no longer pending",
                },
            )
        return {"resolved": True}

    @app.get(
        "/v1/sessions/{session_id}/events",
        dependencies=[dependency],
        tags=["siwah"],
        responses={200: {"content": {"text/event-stream": {}}}},
    )
    async def events(
        session_id: str,
        after: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        daemon = require_runtime()
        if daemon.get_session(session_id) is None:
            raise HTTPException(404, detail={"code": "session_not_found", "message": session_id})
        if last_event_id:
            with contextlib.suppress(ValueError):
                after = max(after, int(last_event_id))
        bus = daemon.bus(session_id)
        if bus.replay_gap(after):
            raise HTTPException(
                409,
                detail={
                    "code": "replay_unavailable",
                    "message": (
                        "Requested event cursor is outside the retained replay window; "
                        "reload the session."
                    ),
                    "last_seq": bus.last_seq(),
                },
            )

        async def stream():
            try:
                async for event in bus.subscribe(after):
                    yield sse_frame(event)
            except Exception:
                return

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post(
        "/v1/sessions/{session_id}/abort",
        response_model=OkResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def abort(session_id: str) -> OkResponse:
        daemon = require_runtime()
        if daemon.get_session(session_id) is None:
            raise HTTPException(404, detail={"code": "session_not_found", "message": session_id})
        aborted = await daemon.abort(session_id)
        return OkResponse(ok=aborted)

    @app.get(
        "/v1/permissions/mode",
        response_model=PermissionModeResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def get_permission_mode() -> PermissionModeResponse:
        return PermissionModeResponse(mode=require_runtime().permission_mode())

    @app.post(
        "/v1/permissions/mode",
        response_model=PermissionModeResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def set_permission_mode(body: PermissionModeRequest) -> PermissionModeResponse:
        return PermissionModeResponse(mode=require_runtime().set_permission_mode(body.mode))

    @app.post(
        "/v1/sessions/{session_id}/permissions/{request_id}",
        response_model=PermissionDecisionResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def permission_decision(
        session_id: str, request_id: str, body: PermissionDecisionRequest
    ) -> PermissionDecisionResponse:
        daemon = require_runtime()
        if daemon.get_session(session_id) is None:
            raise HTTPException(404, detail={"code": "session_not_found", "message": session_id})
        accepted = daemon.resolve_permission(
            request_id, body.decision, body.reason, session_id=session_id
        )
        if not accepted:
            raise HTTPException(404, detail={"code": "permission_not_found", "message": request_id})
        return PermissionDecisionResponse(
            request_id=request_id, decision=body.decision, accepted=True
        )

    @app.get(
        "/v1/sessions/{session_id}/question",
        response_model=QuestionRequest | None,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def pending_question(session_id: str) -> QuestionRequest | None:
        daemon = require_runtime()
        if daemon.get_session(session_id) is None:
            raise HTTPException(404, detail={"code": "session_not_found", "message": session_id})
        return daemon.pending_question(session_id)

    @app.post(
        "/v1/sessions/{session_id}/questions/{request_id}",
        response_model=OkResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def reply_question(session_id: str, request_id: str, body: QuestionReply) -> OkResponse:
        daemon = require_runtime()
        try:
            accepted = daemon.answer_question(session_id, request_id, body.answer)
        except ValueError as exc:
            raise HTTPException(
                400, detail={"code": "invalid_answer", "message": str(exc)}
            ) from exc
        if not accepted:
            raise HTTPException(404, detail={"code": "question_not_found", "message": request_id})
        return OkResponse()

    @app.get(
        "/v1/sessions/{session_id}/checkpoints",
        response_model=CheckpointListResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def checkpoints(session_id: str) -> CheckpointListResponse:
        daemon = require_runtime()
        if daemon.get_session(session_id) is None:
            raise HTTPException(404, detail={"code": "session_not_found", "message": session_id})
        return CheckpointListResponse(
            checkpoints=[checkpoint_summary(record) for record in daemon.checkpoints(session_id)]
        )

    @app.get(
        "/v1/sessions/{session_id}/changes",
        response_model=ChangesResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def changes(
        session_id: str,
        scope: str = "turn",
        checkpoint_id: str | None = None,
        end_checkpoint_id: str | None = None,
        file_path: str | None = None,
    ) -> ChangesResponse:
        if scope not in {"turn", "workspace"}:
            raise HTTPException(status_code=400, detail="Unknown changes scope")
        daemon = require_runtime()
        if daemon.store.get_session(session_id) is None:
            raise HTTPException(status_code=404, detail="Session not found")
        try:
            data = await asyncio.to_thread(
                daemon.changes,
                session_id,
                scope=scope,
                checkpoint_id=checkpoint_id,
                end_checkpoint_id=end_checkpoint_id,
                file_path=file_path,
            )
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(status_code=400, detail="Changes are unavailable.") from exc
        return ChangesResponse.model_validate(data)

    @app.post(
        "/v1/sessions/{session_id}/checkpoints",
        response_model=CheckpointSummary,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def create_checkpoint(
        session_id: str, body: CheckpointCreateRequest
    ) -> CheckpointSummary:
        daemon = require_runtime()
        if daemon.get_session(session_id) is None:
            raise HTTPException(404, detail={"code": "session_not_found", "message": session_id})
        try:
            record = await asyncio.to_thread(daemon.create_checkpoint, session_id, body.label)
        except ValueError as exc:
            raise HTTPException(
                400, detail={"code": "checkpoint_failed", "message": str(exc)}
            ) from exc
        return checkpoint_summary(record)

    @app.post(
        "/v1/sessions/{session_id}/rewind",
        response_model=RewindResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def rewind(session_id: str, body: RewindRequest) -> RewindResponse:
        daemon = require_runtime()
        if daemon.get_session(session_id) is None:
            raise HTTPException(404, detail={"code": "session_not_found", "message": session_id})
        try:
            record = await asyncio.to_thread(
                daemon.rewind,
                session_id,
                checkpoint_id=body.checkpoint_id,
                steps=body.steps,
                confirm=body.confirm,
            )
        except ConfirmationRequired as exc:
            raise HTTPException(
                409,
                detail={
                    "code": "confirmation_required",
                    "message": str(exc),
                    "checkpoint": checkpoint_summary(exc.checkpoint).model_dump(mode="json"),
                },
            ) from exc
        except ValueError as exc:
            raise HTTPException(400, detail={"code": "rewind_failed", "message": str(exc)}) from exc
        return RewindResponse(checkpoint=checkpoint_summary(record))

    @app.post(
        "/v1/sessions/{session_id}/compact",
        response_model=CompactResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def compact(session_id: str) -> CompactResponse:
        daemon = require_runtime()
        if daemon.get_session(session_id) is None:
            raise HTTPException(404, detail={"code": "session_not_found", "message": session_id})
        try:
            result = await daemon.compact_session(session_id)
        except ValueError as exc:
            raise HTTPException(
                400, detail={"code": "compact_failed", "message": str(exc)}
            ) from exc
        return CompactResponse.model_validate(result)

    @app.get(
        "/v1/sessions/{session_id}/plan",
        response_model=PlanResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def plan(session_id: str) -> PlanResponse:
        daemon = require_runtime()
        if daemon.get_session(session_id) is None:
            raise HTTPException(404, detail={"code": "session_not_found", "message": session_id})
        try:
            result = daemon.plan(session_id)
        except ValueError as exc:
            raise HTTPException(400, detail={"code": "plan_failed", "message": str(exc)}) from exc
        return PlanResponse.model_validate(result)

    @app.get(
        "/v1/sessions/{session_id}/tool_calls/{tool_call_id}",
        response_model=ToolOutputResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def tool_output(session_id: str, tool_call_id: str) -> ToolOutputResponse:
        daemon = require_runtime()
        if daemon.get_session(session_id) is None:
            raise HTTPException(404, detail={"code": "session_not_found", "message": session_id})
        try:
            result = daemon.tool_output(session_id, tool_call_id)
        except ValueError as exc:
            raise HTTPException(
                404, detail={"code": "tool_call_not_found", "message": str(exc)}
            ) from exc
        return ToolOutputResponse.model_validate(result)

    @app.get(
        "/v1/evolution",
        response_model=EvolutionSummary,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def evolution() -> EvolutionSummary:
        state = load_state()
        events = [
            event
            for event in read_events(limit=200)
            if str(event.get("event", "")).startswith("evolve.")
        ][-50:]
        observations = (
            runtime.store.compare_run_observations(
                before=state.canary_previous_tag or "baseline",
                after=state.canary_tag,
                min_sessions=runtime.config.canary.sessions,
            )
            if runtime is not None and state.canary_tag
            else []
        )
        return EvolutionSummary(
            observations=observations,
            tag=state.tag,
            status=state.status,
            paused=is_paused(),
            events=events,
        )

    @app.post(
        "/v1/evolution/pause",
        response_model=OkResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def evolution_pause(request: EvolutionPauseRequest) -> OkResponse:
        set_paused(request.paused)
        audit_event("evolve.paused" if request.paused else "evolve.resumed", {"by": "api"})
        return OkResponse()

    @app.post(
        "/v1/evolution/rollback",
        response_model=EvolutionRollbackResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def evolution_rollback(request: EvolutionRollbackRequest) -> EvolutionRollbackResponse:
        repo = HarnessRepository(harness_dir())
        target = request.tag or repo.previous_revision()
        if target is None:
            audit_event("evolve.rollback_manual", {"ok": False, "reason": "no prior revision"})
            raise HTTPException(
                400,
                detail={
                    "code": "evolution_rollback_unavailable",
                    "message": (
                        "No previous harness revision yet. /rollback works after "
                        "/evolve promotes a candidate or after an earlier harness "
                        "commit exists."
                    ),
                },
            )
        try:
            commit = repo.rollback(target)
        except Exception as exc:
            audit_event("evolve.rollback_manual", {"tag": target, "ok": False, "detail": str(exc)})
            raise HTTPException(
                400,
                detail={"code": "evolution_rollback_failed", "message": str(exc)},
            ) from exc
        audit_event("evolve.rollback_manual", {"tag": target, "ok": True, "commit": commit})
        return EvolutionRollbackResponse(ok=True, tag=target, commit=commit)

    @app.get(
        "/v1/commands",
        response_model=CommandListResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def commands(cwd: str | None = Query(default=None)) -> CommandListResponse:
        workspace = Path(cwd).expanduser() if cwd else None
        return CommandListResponse(
            commands=[
                CommandInfo(name=command.name, description=command.description, body=command.body)
                for command in list_commands_for(workspace)
            ]
        )

    @app.get(
        "/v1/events/schema",
        response_model=EventSchemaResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def event_schema() -> EventSchemaResponse:
        return EventSchemaResponse()

    @app.get(
        "/v1/models",
        response_model=ModelCatalogResponse,
        dependencies=[dependency],
        tags=["siwah"],
    )
    async def models(provider: str | None = Query(default=None)) -> ModelCatalogResponse:
        daemon = require_runtime()
        roles = daemon.config.roles.model_dump(exclude_none=True)
        roles.pop("fallback", None)

        def summaries(models: list[ModelInfo]) -> list[ModelSummary]:
            return [
                ModelSummary(
                    id=model.id,
                    name=model.name,
                    context_window=model.context_window,
                    max_output=model.max_output,
                    supports_tools=model.supports_tools,
                    supports_reasoning=model.supports_reasoning,
                    efforts=model.efforts,
                    native_api=model.native_api,
                    privacy=model.privacy,
                    pricing=model.pricing,
                )
                for model in models
            ]

        if provider:
            if not daemon.has_credential(provider):
                return ModelCatalogResponse(
                    providers=[
                        ProviderModels(provider=provider, error="credential missing; run /connect")
                    ],
                    roles=roles,
                )
            try:
                return ModelCatalogResponse(
                    providers=[
                        ProviderModels(
                            provider=provider, models=summaries(await daemon.list_models(provider))
                        )
                    ],
                    roles=roles,
                )
            except (ConfigError, ProviderError) as exc:
                return ModelCatalogResponse(
                    providers=[ProviderModels(provider=provider, error=str(exc))],
                    roles=roles,
                )

        async def load(name: str) -> ProviderModels:
            if not daemon.has_credential(name):
                return ProviderModels(provider=name, error="credential missing; run /connect")
            try:
                return ProviderModels(
                    provider=name, models=summaries(await daemon.list_models(name))
                )
            except (ConfigError, ProviderError) as exc:
                return ProviderModels(provider=name, error=str(exc))

        providers = await asyncio.gather(*(load(name) for name in daemon.config.providers))
        return ModelCatalogResponse(providers=list(providers), roles=roles)

    return app


def error_payload(code: str, message: str, *, retryable: bool = False) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "retryable": retryable}}


__all__ = ["create_app", "error_payload"]
