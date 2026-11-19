"""HTTP and SSE schemas. These are the source for the generated TypeScript types."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..core.types import ToolCall


class Capabilities(BaseModel):
    sse_resume: bool = True
    permission_modes: list[str] = Field(default_factory=lambda: ["ask", "auto-edit", "yolo"])
    features: list[str] = Field(default_factory=list)


class MetaResponse(BaseModel):
    name: str = "siwah"
    version: str
    protocol: str = "0.1"
    daemon_started_at: str
    default_effort: str | None = None
    capabilities: Capabilities


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cwd: str | None = None
    model: str | None = None
    mode: Literal["build", "plan"] = "build"
    title: str | None = None


class SessionResponse(BaseModel):
    id: str
    title: str | None = None
    cwd: str
    model: str | None = None
    mode: str
    status: str
    created_at: str
    updated_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    last_event_seq: int | None = None


class SessionListResponse(BaseModel):
    sessions: list[SessionResponse]


class MessageDiff(BaseModel):
    path: str
    patch: str


class MessageAttachment(BaseModel):
    mime: str
    data: str = Field(max_length=16_000_000)
    name: str | None = None


class MessageView(BaseModel):
    id: str
    seq: int
    role: str
    content: str | None = None
    reasoning: str | None = None
    attachments: list[MessageAttachment] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    tool_outputs: dict[str, str] = Field(default_factory=dict)
    tool_edited: dict[str, bool] = Field(default_factory=dict)
    diffs: list[MessageDiff] = Field(default_factory=list)
    created_at: str


class MessageListResponse(BaseModel):
    messages: list[MessageView]


class UsageResponse(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    allowance: float = 0.0
    cost: float = 0.0
    list_cost: float = 0.0
    steps: int = 0
    context_window: int | None = None
    context_tokens: int | None = None
    context_percent: float | None = None


class McpServerStatus(BaseModel):
    name: str
    transport: str
    description: str = ""
    enabled: bool = True
    valid: bool = True
    status: str
    tools: list[str] = Field(default_factory=list)
    latency_ms: float | None = None
    error: str | None = None


class McpStatusResponse(BaseModel):
    servers: list[McpServerStatus] = Field(default_factory=list)


class UpdateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    mode: Literal["build", "plan"] | None = None
    title: str | None = None
    effort: Literal["minimal", "low", "medium", "high", "xhigh", "max", "off"] | None = None


class EffortRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effort: Literal["minimal", "low", "medium", "high", "xhigh", "max", "off"] | None = None


class EffortResponse(BaseModel):
    effort: str | None = None


class PermissionModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["ask", "auto-edit", "yolo"]


class PermissionModeResponse(BaseModel):
    mode: str


class ForkSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    checkpoint_id: str | None = None


class ForkSessionResponse(BaseModel):
    session: SessionResponse


class FileListResponse(BaseModel):
    files: list[str] = Field(default_factory=list)


class SkillInfo(BaseModel):
    name: str
    description: str = ""


class SkillListResponse(BaseModel):
    skills: list[SkillInfo] = Field(default_factory=list)


class SkillContentResponse(SkillInfo):
    content: str = ""
    path: str | None = None


class MemoryFileInfo(BaseModel):
    path: str
    content: str = ""
    exists: bool = False


class MemoryResponse(BaseModel):
    user: MemoryFileInfo
    project: MemoryFileInfo
    quick: MemoryFileInfo


class InitMemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cwd: str


class InitMemoryResponse(BaseModel):
    path: str
    created: bool
    content: str


class QuickNoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=8000)


class QuickNoteResponse(BaseModel):
    path: str
    changed: bool


class AuthProviderStatus(BaseModel):
    provider: str
    scheme: str = "bearer"
    env: str | None = None
    env_set: bool = False
    file_set: bool = False


class AuthStatusResponse(BaseModel):
    providers: list[AuthProviderStatus] = Field(default_factory=list)


class AuthLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    api_key: str = Field(min_length=1)


class ShellRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1)
    approved: bool = False
    timeout_s: float = 120.0


class ShellResponse(BaseModel):
    ok: bool
    output: str = ""
    exit_code: int | None = None
    permission_required: bool = False
    preview: str | None = None


class SendMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = ""
    attachments: list[MessageAttachment] = Field(default_factory=list)

    @model_validator(mode="after")
    def _content_or_attachment(self) -> SendMessageRequest:
        if not self.content.strip() and not self.attachments:
            raise ValueError("content or attachments is required")
        return self


class SendMessageResponse(BaseModel):
    session_id: str
    run_id: str
    event_id: int
    status: str = "running"


class PermissionDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["once", "session", "always", "deny"]
    reason: str | None = None


class PermissionDecisionResponse(BaseModel):
    request_id: str
    decision: str
    accepted: bool


class ModelSummary(BaseModel):
    id: str
    name: str | None = None
    context_window: int | None = None
    max_output: int | None = None
    supports_tools: bool | None = None
    supports_reasoning: bool | None = None
    efforts: list[str] = Field(default_factory=list)
    native_api: str | None = None
    privacy: dict[str, Any] = Field(default_factory=dict)
    pricing: dict[str, Any] = Field(default_factory=dict)


class ProviderModels(BaseModel):
    provider: str
    models: list[ModelSummary] = Field(default_factory=list)
    error: str | None = None


class ModelCatalogResponse(BaseModel):
    providers: list[ProviderModels] = Field(default_factory=list)
    roles: dict[str, str] = Field(default_factory=dict)


class EventData(BaseModel):
    """Typed envelope for SSE data. The event type selects the fields."""

    model_config = ConfigDict(extra="allow")


class EventEnvelope(BaseModel):
    v: int = 1
    id: str
    seq: int
    type: str
    session_id: str
    turn_id: str | None = None
    ts: str
    data: dict[str, Any] = Field(default_factory=dict)


class EventSchemaResponse(BaseModel):
    """Schema introspection: the event envelope shape used by the SSE stream."""

    events: list[EventEnvelope] = Field(default_factory=list)


class ErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class CheckpointSummary(BaseModel):
    id: str
    label: str | None = None
    commit: str | None = None
    created_at: str


class CheckpointListResponse(BaseModel):
    checkpoints: list[CheckpointSummary] = Field(default_factory=list)


class CheckpointCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = None


class RewindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checkpoint_id: str | None = None
    steps: int = 1
    confirm: bool = False


class RewindResponse(BaseModel):
    ok: bool = True
    checkpoint: CheckpointSummary


class CompactResponse(BaseModel):
    summary: str
    through_seq: int
    before_tokens: int
    after_tokens: int


class PlanResponse(BaseModel):
    path: str
    content: str = ""
    exists: bool = False


class ToolOutputResponse(BaseModel):
    tool_call_id: str
    name: str
    output: str = ""
    handle: str | None = None


class EvolutionSummary(BaseModel):
    tag: str | None = None
    status: str = "idle"
    paused: bool = False
    events: list[dict[str, Any]] = Field(default_factory=list)


class EvolutionPauseRequest(BaseModel):
    paused: bool = True


class EvolutionRollbackRequest(BaseModel):
    tag: str | None = None


class EvolutionRollbackResponse(BaseModel):
    ok: bool = True
    tag: str | None = None
    commit: str | None = None


class CommandInfo(BaseModel):
    name: str
    description: str
    body: str


class CommandListResponse(BaseModel):
    commands: list[CommandInfo] = Field(default_factory=list)


class UpdateStatusResponse(BaseModel):
    current: str
    latest: str | None = None
    available: bool = False
    repo: str | None = None
    branch: str | None = None
    source: str | None = None
    commits_behind: int = 0
    checked_at: str = ""
    error: str | None = None
    can_apply: bool = False
    detail: str | None = None


class UpdateApplyResponse(BaseModel):
    ok: bool
    from_version: str
    to_version: str | None = None
    detail: str = ""
    restart_required: bool = False
    steps: list[str] = Field(default_factory=list)


class OkResponse(BaseModel):
    ok: bool = True
