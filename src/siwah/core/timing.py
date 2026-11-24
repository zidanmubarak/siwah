"""Measure elapsed wall time across activities in one daemon run."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Awaitable
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter
from typing import TypeVar

T = TypeVar("T")
# Permission waits can occur inside a tool or verification. Parallel tools
# share one elapsed clock rather than accumulating independent durations.
PHASES = ("permission", "hook", "checkpoint", "verifier", "model", "tool")


@dataclass
class RunTiming:
    started: float = field(default_factory=perf_counter)
    boundaries: list[tuple[float, str, int]] = field(default_factory=list)

    def snapshot(self) -> dict[str, float]:
        ended = perf_counter()
        elapsed: defaultdict[str, float] = defaultdict(float)
        active: Counter[str] = Counter()
        previous = self.started
        for moment, phase, delta in sorted([*self.boundaries, (ended, "other", 0)]):
            bucket = next((name for name in PHASES if active[name]), "other")
            elapsed[bucket] += moment - previous
            active[phase] += delta
            previous = moment
        return {
            "total": round((ended - self.started) * 1000, 2),
            **{phase: round(elapsed[phase] * 1000, 2) for phase in (*PHASES, "other")},
        }


active_timing: ContextVar[RunTiming | None] = ContextVar("active_timing", default=None)


async def timed(phase: str, operation: Awaitable[T]) -> T:
    timing = active_timing.get()
    if timing is None:
        return await operation
    start = perf_counter()
    try:
        return await operation
    finally:
        timing.boundaries.extend([(start, phase, 1), (perf_counter(), phase, -1)])
