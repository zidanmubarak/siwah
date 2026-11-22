"""Privacy-gated reflector for evolution observations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from ..kernel.secrets import redact_secrets
from ..providers.profiles import ModelProfile, load_profile
from .models import Observation, Reflection
from .observer import observation_payload

ModelCall = Callable[[str], Awaitable[str]]
ProfileLoader = Callable[[str, str], ModelProfile]


class PrivacyError(RuntimeError):
    """Raised when a model cannot be used for reflection."""


class ReflectorProtocol(Protocol):
    async def reflect(
        self,
        model: str,
        observations: list[Observation],
        *,
        signals: list[str] | None = None,
    ) -> Reflection: ...


class Reflector:
    def __init__(
        self,
        call: ModelCall,
        *,
        profile_loader: ProfileLoader = load_profile,
        model_used: Callable[[], str | None] | None = None,
    ) -> None:
        self.call = call
        self.profile_loader = profile_loader
        self.model_used = model_used

    def ensure_training_safe(self, model: str) -> ModelProfile:
        provider, separator, model_id = model.partition("/")
        if not separator:
            raise PrivacyError(f"invalid model reference: {model}")
        try:
            profile = self.profile_loader(provider, model_id)
        except (OSError, ValueError) as exc:
            raise PrivacyError(
                f"privacy profile is missing for {model}; run `siwah models probe {model}` first"
            ) from exc
        if profile.privacy.training is not False:
            raise PrivacyError(
                f"model {model} is not marked training=false; "
                f"verify the provider contract, then run "
                f"`siwah models privacy {model} --training false`"
            )
        return profile

    async def reflect(
        self,
        model: str,
        observations: list[Observation],
        *,
        signals: list[str] | None = None,
    ) -> Reflection:
        self.ensure_training_safe(model)
        payload = redact_secrets(observation_payload(observations))
        prompt = (
            "You are a harness reflector. Analyze the following redacted coding "
            "sessions and identify one concrete, low-risk improvement to the "
            "system prompt or tool guidance. Do not propose changes to the kernel, "
            "evals, permissions, or guardrails. Return a concise diagnosis.\n\n"
            f"Signals: {signals or []}\n\nSessions:\n{payload}"
        )
        text = redact_secrets(await self.call(prompt)).strip()
        if not text:
            raise PrivacyError("reflector returned an empty diagnosis")
        actual_model = self.model_used() if self.model_used is not None else None
        return Reflection(summary=text, signals=list(signals or []), model=actual_model or model)


__all__ = ["ModelCall", "PrivacyError", "Reflector", "ReflectorProtocol"]
