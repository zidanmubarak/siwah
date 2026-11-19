"""Compose non-conflicting candidates and re-run the gate."""

from __future__ import annotations

from .candidates import Candidate, ProbeResult
from .gate import GateDecision, evaluate_gate
from .policy import GateConfig
from .prober import ProberProtocol


def non_conflicting(candidates: list[Candidate]) -> list[Candidate]:
    accepted: list[Candidate] = []
    used: set[str] = set()
    scope: tuple[str, str | None] | None = None
    for candidate in candidates:
        if scope is None:
            scope = (candidate.scope, candidate.model)
        elif scope != (candidate.scope, candidate.model):
            continue
        paths = set(candidate.target_paths)
        if paths & used:
            continue
        accepted.append(candidate)
        used.update(paths)
    return accepted


def merge_candidates(candidates: list[Candidate], *, candidate_id: str = "composed") -> Candidate:
    if not candidates:
        raise ValueError("cannot compose an empty candidate list")
    changes: dict[str, str | None] = {}
    paths: list[str] = []
    evidence: list[str] = []
    for candidate in candidates:
        changes.update(candidate.changes)
        paths.extend(candidate.target_paths)
        evidence.extend(candidate.evidence)
    return Candidate(
        id=candidate_id,
        level=max(candidate.level for candidate in candidates),
        kind="composed",
        scope=candidates[0].scope,
        model=candidates[0].model,
        target_paths=paths,
        changes=changes,
        rationale="; ".join(candidate.rationale for candidate in candidates),
        evidence=evidence,
        expected_metric="combined gain",
        risk="composition of multiple changes",
    )


class Composer:
    def __init__(self, prober: ProberProtocol, gate: GateConfig) -> None:
        self.prober = prober
        self.gate = gate

    async def compose(
        self,
        candidates: list[Candidate],
        baseline: ProbeResult,
        harness_root: str,
    ) -> tuple[Candidate | None, ProbeResult | None, GateDecision | None]:
        pool = non_conflicting(candidates)
        if not pool:
            return None, None, None
        if len(pool) == 1:
            candidate = pool[0]
            probe = await self.prober.probe(candidate, harness_root, tier="t2")
            decision = evaluate_gate(baseline.golden, probe.golden, self.gate)
            return (candidate, probe, decision) if decision.accepted else (None, probe, decision)

        combined = merge_candidates(pool)
        probe = await self.prober.probe(combined, harness_root, tier="t2")
        decision = evaluate_gate(baseline.golden, probe.golden, self.gate)
        if decision.accepted:
            return combined, probe, decision

        for candidate in pool:
            single = await self.prober.probe(candidate, harness_root, tier="t2")
            single_decision = evaluate_gate(baseline.golden, single.golden, self.gate)
            if single_decision.accepted:
                return candidate, single, single_decision
        return None, probe, decision


__all__ = ["Composer", "merge_candidates", "non_conflicting"]
