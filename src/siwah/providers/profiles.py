"""Model capability profiles and probing."""

from __future__ import annotations

import hashlib
import re
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import tomli_w
from pydantic import BaseModel, ConfigDict, Field

from ..paths import profiles_dir

if TYPE_CHECKING:
    from .base import ProviderAdapter


class PrivacyProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    training: bool | None = None
    retention_days: int | None = None


class ModelQuirks(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tool_call_repair: bool = True
    text_tool_protocol: bool = False
    tool_error_behavior: str = "unknown"
    json_compliance: bool | None = None


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    provider: str
    model: str
    api: str
    supports_tools: bool = False
    parallel_tool_calls: bool = False
    reasoning_field: str | None = None
    context_window: int | None = None
    max_output: int | None = None
    edit_format: str = "search_replace"
    cache_mode: str = "none"
    privacy: PrivacyProfile = Field(default_factory=PrivacyProfile)
    pricing: dict[str, Any] = Field(default_factory=dict)
    quirks: ModelQuirks = Field(default_factory=ModelQuirks)
    probed_at: str | None = None

    @property
    def uses_text_tool_protocol(self) -> bool:
        return not self.supports_tools and self.quirks.text_tool_protocol


_UNSAFE_PROFILE_CHARS = frozenset('<>:"|?*')
_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


def _safe_profile_directory(name: str) -> bool:
    if not name or name != name.rstrip(" ."):
        return False
    if any(char in _UNSAFE_PROFILE_CHARS or ord(char) < 32 for char in name):
        return False
    return name.split(".")[0].casefold() not in _RESERVED_NAMES


def _profile_directory(provider: str, model: str) -> str:
    raw = f"{provider}__{model}"
    legacy = raw.replace("/", "__").replace("\\", "__")
    if _safe_profile_directory(legacy):
        return legacy
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._-")[:80] or "model"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{slug}_{digest}"


def profile_path(provider: str, model: str, root: Path | None = None) -> Path:
    return (root or profiles_dir()) / _profile_directory(provider, model) / "model.toml"


def save_profile(profile: ModelProfile, root: Path | None = None) -> Path:
    path = profile_path(profile.provider, profile.model, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = profile.model_dump(mode="json", exclude_none=True)
    path.write_text(tomli_w.dumps(payload), encoding="utf-8")
    return path


def load_profile(provider: str, model: str, root: Path | None = None) -> ModelProfile:
    path = profile_path(provider, model, root)
    return ModelProfile.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))


def merge_privacy(profile: ModelProfile, previous: ModelProfile | None) -> ModelProfile:
    """Keep a human privacy assertion when a fresh probe has no privacy data."""

    if previous is None:
        return profile
    training = (
        profile.privacy.training
        if profile.privacy.training is not None
        else previous.privacy.training
    )
    retention = (
        profile.privacy.retention_days
        if profile.privacy.retention_days is not None
        else previous.privacy.retention_days
    )
    return profile.model_copy(
        update={"privacy": PrivacyProfile(training=training, retention_days=retention)}
    )


def set_privacy(
    provider: str,
    model: str,
    *,
    training: bool | None,
    retention_days: int | None = None,
    root: Path | None = None,
) -> tuple[ModelProfile, Path]:
    """Persist an explicit privacy assertion without re-probing the model."""

    profile = load_profile(provider, model, root)
    resolved_retention = (
        retention_days if retention_days is not None else profile.privacy.retention_days
    )
    updated = profile.model_copy(
        update={
            "privacy": PrivacyProfile(
                training=training,
                retention_days=resolved_retention,
            )
        }
    )
    return updated, save_profile(updated, root)


async def probe_model(
    adapter: ProviderAdapter,
    provider: str,
    model: str,
    *,
    model_info: dict[str, Any] | None = None,
) -> ModelProfile:
    """Run a small live capability probe.

    This is intentionally cheap: three short calls. It does not test the full
    effective context window; that value comes from the models endpoint when
    available.
    """
    from ..core.types import ChatRequest, Message, ToolSpec

    emit = _noop_emit
    echo_tool = ToolSpec(
        name="probe_echo",
        description="Echo a value back to the caller. Used only by capability probes.",
        parameters={
            "type": "object",
            "properties": {
                "value": {"type": "string", "description": "Value to echo."},
            },
            "required": ["value"],
            "additionalProperties": False,
        },
    )
    base_messages = [
        Message(
            role="system",
            content="You are a capability probe. Follow the instruction exactly. Do not explain.",
        )
    ]
    tool_outcome = await adapter.stream_chat(
        ChatRequest(
            model=model,
            messages=[
                *base_messages,
                Message(
                    role="user",
                    content="Call probe_echo exactly once with value 'ok'. Do not answer in text.",
                ),
            ],
            tools=[echo_tool],
            max_tokens=128,
        ),
        session_id="probe-tools",
        emit=emit,
    )
    parallel_outcome = await adapter.stream_chat(
        ChatRequest(
            model=model,
            messages=[
                *base_messages,
                Message(
                    role="user",
                    content=(
                        "Call probe_echo twice in one response, with values 'a' and 'b'. "
                        "Do not answer in text."
                    ),
                ),
            ],
            tools=[echo_tool],
            max_tokens=128,
        ),
        session_id="probe-parallel",
        emit=emit,
    )
    json_outcome = await adapter.stream_chat(
        ChatRequest(
            model=model,
            messages=[
                *base_messages,
                Message(role="user", content='Reply with exactly this JSON: {"ok":true}'),
            ],
            max_tokens=64,
        ),
        session_id="probe-json",
        emit=emit,
    )

    supports_tools = bool(tool_outcome.tool_calls)
    parallel = len(parallel_outcome.tool_calls) >= 2
    reasoning_field = None
    cache_mode = "none"
    cache_read = max(
        tool_outcome.usage.cache_read_tokens,
        parallel_outcome.usage.cache_read_tokens,
        json_outcome.usage.cache_read_tokens,
    )
    if cache_read > 0:
        cache_mode = "implicit"
    info = model_info or {}
    context_window = _int_or_none(info.get("context_window"))
    max_output = _int_or_none(info.get("max_output"))
    pricing_raw = info.get("pricing")
    pricing: dict[str, Any] = pricing_raw if isinstance(pricing_raw, dict) else {}
    privacy = _privacy(info.get("privacy"))
    if info.get("reasoning_field"):
        reasoning_field = str(info["reasoning_field"])
    elif tool_outcome.reasoning or parallel_outcome.reasoning:
        reasoning_field = "reasoning_content"
    json_ok = _json_ok(json_outcome.text)
    return ModelProfile(
        provider=provider,
        model=model,
        api=adapter.api,
        supports_tools=supports_tools,
        parallel_tool_calls=parallel,
        reasoning_field=reasoning_field,
        context_window=context_window,
        max_output=max_output,
        cache_mode=cache_mode,
        privacy=privacy,
        pricing=pricing,
        quirks=ModelQuirks(
            tool_call_repair=any(call.raw_arguments for call in tool_outcome.tool_calls),
            text_tool_protocol=False,
            tool_error_behavior="unknown",
            json_compliance=json_ok,
        ),
    )


async def _noop_emit(_event) -> None:
    return None


def _json_ok(text: str) -> bool:
    import json

    try:
        parsed = json.loads(text.strip())
    except json.JSONDecodeError:
        return False
    return parsed == {"ok": True}


def _privacy(value: Any) -> PrivacyProfile:
    if not isinstance(value, dict):
        return PrivacyProfile()
    training = value.get("training")
    retention = value.get("retention_days")
    return PrivacyProfile(
        training=training if isinstance(training, bool) else None,
        retention_days=_int_or_none(retention),
    )


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = [
    "ModelProfile",
    "PrivacyProfile",
    "load_profile",
    "probe_model",
    "profile_path",
    "save_profile",
]
