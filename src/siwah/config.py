"""Configuration loading and model references.

The config is deliberately serde-simple: it is a TOML file under
`~/.siwah/harness/config.toml` (or `SIWAH_CONFIG`). Legacy
`~/.siwah/config.toml` files are migrated on first load. No model name, price,
or limit is embedded in code.
"""

from __future__ import annotations

import contextlib
import os
import tomllib
from pathlib import Path
from typing import Any, Literal

import tomli_w
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .kernel.policy import BudgetConfig, CanaryConfig, GateConfig
from .paths import config_path, ensure_home, legacy_config_path, write_private


class ConfigError(RuntimeError):
    """Raised when the config is missing or cannot be used for a request."""


class AuthConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scheme: Literal["bearer", "none"] = "bearer"
    env: str | None = None
    browser: Literal["openrouter", "chatgpt"] | None = None


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    api: str = "openai_chat"
    base_url: str
    models_url: str | None = None
    auth: AuthConfig = Field(default_factory=AuthConfig)
    headers: dict[str, str] = Field(default_factory=dict)
    stream_usage: bool = True
    cost_field: str = "usage.opencode.allowance"
    reasoning_format: Literal["flat", "openrouter"] = "flat"
    replay_reasoning: bool = False
    max_retries: int = 2
    timeout_s: float = 120.0
    connect_timeout_s: float = 15.0

    @field_validator("base_url", "models_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.rstrip("/")


class PermissionRule(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tool: str | None = None
    path: str | None = None
    command: str | None = None
    decision: Literal["allow", "ask", "deny"]


class PermissionsConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: Literal["ask", "auto-edit", "yolo"] = "ask"
    rules: list[PermissionRule] = Field(default_factory=list)
    deny_paths: list[str] = Field(
        default_factory=lambda: [
            ".env",
            "**/.env",
            ".env.*",
            "**/.env.*",
            "**/.ssh/**",
            "**/id_rsa",
            "**/id_ed25519",
            "**/.aws/credentials",
            "**/.config/gcloud/**",
            "**/credentials.json",
        ]
    )


EFFORT_VALUES: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh", "max")


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # `max_steps` is the hard budget for explicit/non-interactive callers
    # (CLI `--max-steps`, eval, subagents). Interactive runs use
    # `interactive_max_steps`; 0 there means no step cap, while a positive value
    # adds progress-based extensions up to `max_steps_ceiling`.
    max_steps: int = 50
    interactive_max_steps: int = 0
    step_extension: int = 50
    max_steps_ceiling: int = 300
    stall_limit: int = 8
    max_completion_nudges: int = 2
    self_check_min_steps: int = 12
    tool_output_limit: int = 100_000
    bash_timeout_s: float = 120.0
    web_timeout_s: float = 30.0
    max_retries: int = 2
    effort: Literal["minimal", "low", "medium", "high", "xhigh", "max"] | None = None


class VerifyConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    auto: bool = True
    timeout_s: float = 120.0
    max_repair_steps: int = 3
    commands: dict[str, str] = Field(default_factory=dict)
    retry_flaky: bool = True
    keep_output_chars: int = 6_000


class ContextConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    keep_recent_tool_results: int = 50
    auto_compact_percent: float = 95.0
    max_context_tokens: int | None = None
    summary_max_chars: int = 32_000


class CheckpointConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True


class RolesConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    main: str | None = None
    plan: str | None = None
    fast: str | None = None
    reflector: str | None = None
    eval_screen: str | None = None
    fallback: list[str] = Field(default_factory=list)


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    host: str = "127.0.0.1"
    port: int = 0
    event_retention: int = 2_000


class EvolutionConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    auto: bool = True
    min_sessions: int = 3
    cooldown_minutes: int = 60


class SubagentConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    max_steps: int = 8
    max_allowance: float = 0.2
    max_depth: int = 1


class JobsConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_jobs: int = 8
    output_limit: int = 20_000
    tail_lines: int = 200


class EscalationConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    after_failures: int = 2
    model: str | None = None


class TraceConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    max_events: int = 2_000


class SandboxConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    backend: Literal["auto", "docker", "bwrap", "local"] = "auto"
    image: str = ""
    network: bool = False


class Config(BaseModel):
    model_config = ConfigDict(extra="ignore")

    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    roles: RolesConfig = Field(default_factory=RolesConfig)
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    verify: VerifyConfig = Field(default_factory=VerifyConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    checkpoints: CheckpointConfig = Field(default_factory=CheckpointConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    gate: GateConfig = Field(default_factory=GateConfig)
    canary: CanaryConfig = Field(default_factory=CanaryConfig)
    evolution: EvolutionConfig = Field(default_factory=EvolutionConfig)
    subagents: SubagentConfig = Field(default_factory=SubagentConfig)
    jobs: JobsConfig = Field(default_factory=JobsConfig)
    escalation: EscalationConfig = Field(default_factory=EscalationConfig)
    trace: TraceConfig = Field(default_factory=TraceConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)

    def model_for_role(self, role: str, override: str | None = None) -> ModelRef:
        reference = override or getattr(self.roles, role, None)
        if not reference:
            raise ConfigError(
                f"No model configured for role '{role}'. Set --model or "
                f"[roles].{role} in {config_path()}. The TUI can choose a "
                f"model per session with /models."
            )
        return ModelRef.parse(reference)

    def fallback_refs(self) -> list[ModelRef]:
        refs: list[ModelRef] = []
        for value in self.roles.fallback:
            try:
                refs.append(ModelRef.parse(value))
            except ValueError:
                continue
        return refs


class ModelRef(BaseModel):
    """A `provider/model-id` reference."""

    model_config = ConfigDict(frozen=True)

    provider: str
    model: str

    @classmethod
    def parse(cls, value: str) -> ModelRef:
        provider, separator, model = value.partition("/")
        if not separator or not provider or not model:
            raise ValueError(f"Invalid model reference '{value}'; expected provider/model-id")
        return cls(provider=provider, model=model)

    def __str__(self) -> str:
        return f"{self.provider}/{self.model}"


def default_config_text() -> str:
    """Default first-run config.

    Endpoints are sourced from `docs/EXTERNAL_FACTS.md`; model IDs, prices and
    limits are deliberately not embedded here. `/connect` only needs provider
    metadata, and `/models` reads the live catalog after a key is stored.
    """

    return """# Generated by siwah on first run. Edit to add providers or defaults.
# Model IDs are discovered from the provider's models endpoint; they are not
# hardcoded here. See docs/EXTERNAL_FACTS.md for endpoint sources.

[providers.opencode-go]
api = "openai_chat"
base_url = "https://opencode.ai/inference/go/openai/v1"
models_url = "https://opencode.ai/inference/go/v1/models"
auth = { scheme = "bearer", env = "OPENCODE_API_KEY" }
headers = { "User-Agent" = "siwah/{version}", "x-opencode-session" = "{session_id}" }
stream_usage = true
cost_field = "usage.opencode.allowance"

[providers.openai]
api = "openai_chat"
base_url = "https://api.openai.com/v1"
models_url = "https://api.openai.com/v1/models"
auth = { scheme = "bearer", env = "OPENAI_API_KEY" }
stream_usage = true
cost_field = "usage.cost"

[providers.chatgpt]
api = "openai_responses"
base_url = "https://api.openai.com/v1"
models_url = "https://api.openai.com/v1/models"
auth = { scheme = "bearer", browser = "chatgpt" }

[providers.openrouter]
api = "openai_chat"
base_url = "https://openrouter.ai/api/v1"
models_url = "https://openrouter.ai/api/v1/models"
auth = { scheme = "bearer", env = "OPENROUTER_API_KEY", browser = "openrouter" }
cost_field = "usage.cost"
reasoning_format = "openrouter"

[providers.deepseek]
api = "openai_chat"
base_url = "https://api.deepseek.com"
models_url = "https://api.deepseek.com/models"
auth = { scheme = "bearer", env = "DEEPSEEK_API_KEY" }
replay_reasoning = true

[providers.commandcode]
api = "openai_chat"
base_url = "https://api.commandcode.ai/provider/v1"
models_url = "https://api.commandcode.ai/provider/v1/models"
auth = { scheme = "bearer", env = "COMMAND_CODE_API_KEY" }

[roles]
# main = "opencode-go/<model-id>"
# plan = "opencode-go/<model-id>"
# fast = "opencode-go/<model-id>"
# fallback = ["opencode-go/<model-id>"]

[permissions]
mode = "ask"

[agent]
# Hard cap for explicit/non-interactive budgets: `siwah run --max-steps`, eval,
# and subagents. Interactive TUI runs use `interactive_max_steps` instead.
max_steps = 50
# 0 means no step cap for interactive runs. A positive value enables extensions
# while the agent keeps making progress, up to max_steps_ceiling.
interactive_max_steps = 0
step_extension = 50
max_steps_ceiling = 300
stall_limit = 8
max_completion_nudges = 2
self_check_min_steps = 12

[verify]
auto = true
max_repair_steps = 3

[context]
keep_recent_tool_results = 50
auto_compact_percent = 95

[checkpoints]
enabled = true

[gate]
golden_regression_tolerance = 0.0
heldout_noninferiority = 0.02
min_gain = "pass_rate_up OR (steps_or_cost_down >= 0.10 with equal pass_rate)"
max_cost_ratio = 1.2
runs_per_task = 3

[budget]
# Calibrated by `siwah eval --live ... --write-budget`; the values below are
# spec defaults until a measured run replaces them.
max_allowance_per_cycle = 1.0
max_cycles_per_day = 3
reserve_weekly_pct = 40
max_concurrency = 2
cooldown_minutes = 60
max_wall_minutes = 45

[canary]
sessions = 10
max_error_rate_increase = 0.15
max_correction_rate_increase = 0.15

[evolution]
auto = true
min_sessions = 3
cooldown_minutes = 60

[subagents]
enabled = true
max_steps = 8
max_allowance = 0.2
max_depth = 1

[jobs]
max_jobs = 8
output_limit = 20000
tail_lines = 200

[escalation]
enabled = false
after_failures = 2
# model = "opencode-go/<model-id>"

[trace]
enabled = true
max_events = 2000

[sandbox]
enabled = false
backend = "auto"
# image = "your-sandbox-image"
network = false
"""


def _migrate_legacy_config() -> None:
    target = config_path()
    legacy = legacy_config_path()
    if not legacy.exists() or legacy == target:
        return
    if target.exists():
        with contextlib.suppress(OSError):
            if target.stat().st_size > 0:
                return
    try:
        ensure_home()
        write_private(target, legacy.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"Cannot migrate {legacy} to {target}: {exc}") from exc


def ensure_default_config() -> Path:
    """Create the first-run config with the built-in provider catalog."""

    target = config_path()
    if not target.exists():
        _migrate_legacy_config()
    if target.exists():
        with contextlib.suppress(OSError):
            if target.stat().st_size == 0:
                write_private(target, default_config_text())
        return target
    ensure_home()
    write_private(target, default_config_text())
    return target


def add_default_providers() -> Config:
    """Append missing provider presets without rewriting the user's config."""
    target = ensure_default_config()
    config = load_config(target)
    defaults = tomllib.loads(default_config_text())["providers"]
    missing = {name: preset for name, preset in defaults.items() if name not in config.providers}
    if missing:
        text = target.read_text(encoding="utf-8").rstrip()
        updated = f"{text}\n\n{tomli_w.dumps({'providers': missing})}"
        try:
            config = Config.model_validate(tomllib.loads(updated))
        except ValueError as exc:
            raise ConfigError(
                f"Cannot append provider presets to {target}. Use separate provider tables in TOML."
            ) from exc
        write_private(target, updated)
    return config


def default_provider_names() -> set[str]:
    """Return the provider names shipped in the default preset catalog."""
    return set(tomllib.loads(default_config_text())["providers"])


def update_budget_config(values: dict[str, object], path: Path | None = None) -> Path:
    """Replace the `[budget]` table with calibrated values.

    Other sections and comments are preserved so a calibration run does not
    rewrite credentials or user settings.
    """

    budget = BudgetConfig.model_validate(values)
    target = path or config_path()
    if not target.exists():
        if path is None:
            ensure_default_config()
        else:
            write_private(target, default_config_text())
    text = target.read_text(encoding="utf-8")
    kept: list[str] = []
    skipping = False
    for line in text.splitlines():
        stripped = line.strip()
        header = stripped.split("#", 1)[0].strip()
        if header.startswith("[") and header.endswith("]"):
            skipping = header == "[budget]"
            if skipping:
                continue
        if not skipping:
            kept.append(line)
    body = "\n".join(kept).rstrip() + "\n\n"
    body += tomli_w.dumps({"budget": budget.model_dump()})
    write_private(target, body)
    return target


def update_agent_effort(effort: str | None, path: Path | None = None) -> Path:
    """Persist the default reasoning effort while preserving other settings."""

    normalized = effort.strip().lower() if effort else None
    if normalized in {"", "default", "none", "off"}:
        normalized = None
    if normalized is not None and normalized not in EFFORT_VALUES:
        raise ValueError(f"effort must be one of {', '.join(EFFORT_VALUES)} or off")

    target = path or config_path()
    if not target.exists():
        if path is None:
            ensure_default_config()
        else:
            write_private(target, default_config_text())
    lines = target.read_text(encoding="utf-8").splitlines()
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip().split("#", 1)[0].strip() == "[agent]"
        ),
        None,
    )
    if start is None:
        body = "\n".join(lines).rstrip()
        body += "\n\n[agent]\n"
        if normalized is not None:
            body += f'effort = "{normalized}"\n'
    else:
        end = next(
            (
                index
                for index in range(start + 1, len(lines))
                if lines[index].strip().startswith("[") and lines[index].strip().endswith("]")
            ),
            len(lines),
        )
        section = [line for line in lines[start + 1 : end] if not line.strip().startswith("effort")]
        if normalized is not None:
            section.append(f'effort = "{normalized}"')
        body = "\n".join([*lines[: start + 1], *section, *lines[end:]]).rstrip() + "\n"
    write_private(target, body)
    return target


def load_config(path: Path | None = None) -> Config:
    target = path or config_path()
    if not target.exists():
        if path is None and not os.environ.get("SIWAH_CONFIG"):
            try:
                ensure_default_config()
            except OSError as exc:
                raise ConfigError(f"Cannot create default config {target}: {exc}") from exc
        else:
            return Config()
    try:
        raw: dict[str, Any] = tomllib.loads(target.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot read config {target}: {exc}") from exc
    try:
        return Config.model_validate(raw)
    except Exception as exc:  # pydantic error with a helpful wrapper
        raise ConfigError(f"Invalid config {target}: {exc}") from exc
