"""Post-promotion canary evaluation."""

from __future__ import annotations

from dataclasses import dataclass

from .candidates import CanaryDecision, CanarySample
from .policy import CanaryConfig


@dataclass(frozen=True)
class CanaryBaseline:
    tool_error_rate: float = 0.0
    correction_rate: float = 0.0
    abort_rate: float = 0.0


class CanaryMonitor:
    def __init__(self, config: CanaryConfig, baseline: CanaryBaseline) -> None:
        self.config = config
        self.baseline = baseline
        self.samples: list[CanarySample] = []

    @property
    def ready(self) -> bool:
        return len(self.samples) >= self.config.sessions

    def record(self, sample: CanarySample) -> None:
        self.samples.append(sample)

    def evaluate(self) -> CanaryDecision:
        if not self.samples:
            return CanaryDecision(ok=True, reasons=["no canary samples yet"], sessions=0)
        count = len(self.samples)
        errors = sum(sample.tool_error_rate for sample in self.samples) / count
        corrections = sum(sample.correction_rate for sample in self.samples) / count
        aborts = sum(sample.abort_rate for sample in self.samples) / count
        reasons: list[str] = []
        if errors > self.baseline.tool_error_rate + self.config.max_error_rate_increase:
            reasons.append("tool-error rate exceeded the canary threshold")
        if corrections > self.baseline.correction_rate + self.config.max_correction_rate_increase:
            reasons.append("correction rate exceeded the canary threshold")
        if aborts > self.baseline.abort_rate + self.config.max_error_rate_increase:
            reasons.append("abort rate exceeded the canary threshold")
        return CanaryDecision(ok=not reasons, reasons=reasons, sessions=count)


__all__ = ["CanaryBaseline", "CanaryMonitor"]
