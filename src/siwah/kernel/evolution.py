"""Evolution orchestration: observe -> reflect -> propose -> probe -> promote."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..adapt.observer import Observer
from ..adapt.reflector import PrivacyError, ReflectorProtocol
from .approval import _issue_approval
from .audit import audit_event
from .budget import BudgetGovernor
from .canary import CanaryBaseline, CanaryMonitor
from .candidate_safety import validate_candidate
from .candidates import (
    CanaryDecision,
    CanarySample,
    EvolutionResult,
    EvolutionState,
    Observation,
    ProbeResult,
)
from .composer import Composer
from .evolution_state import is_paused, pause_path, save_state, state_path
from .gate import GateDecision, evaluate_gate
from .manifest import verify_manifest
from .policy import CanaryConfig, GateConfig
from .prober import ProberProtocol
from .promotion import Promoter

if TYPE_CHECKING:
    from ..core.storage import Store

Audit = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class _CycleStart:
    allowance: float
    duration: float
    model_allowance: float
    monotonic: float
    head: str


class EvolutionEngine:
    def __init__(
        self,
        repo_root: Path | str,
        *,
        reflector: ReflectorProtocol,
        proposer: Any,
        prober: ProberProtocol,
        promoter: Promoter,
        governor: BudgetGovernor,
        gate: GateConfig,
        canary: CanaryConfig,
        model: str,
        composer: Composer | None = None,
        store: Store | None = None,
        observer: Observer | None = None,
        audit: Audit = audit_event,
        state_file: Path | None = None,
        pause_file: Path | None = None,
        environ: dict[str, str] | None = None,
        model_allowance: Callable[[], float] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.reflector = reflector
        self.proposer = proposer
        self.prober = prober
        self.promoter = promoter
        self.composer = composer or Composer(prober, gate)
        self.governor = governor
        self.gate = gate
        self.canary_config = canary
        self.model = model
        self.store = store
        self.observer = observer or Observer()
        self.audit = audit
        self.state_file = state_file
        self.pause_file = pause_file
        self.environ = environ if environ is not None else os.environ
        self.model_allowance = model_allowance or (lambda: 0.0)
        self._monitor: CanaryMonitor | None = None
        self._promotion_tag: str | None = None
        self._previous_commit = ""
        self._previous_tag: str | None = None

    async def run_once(
        self,
        observations: list[Observation] | None = None,
        *,
        baseline: ProbeResult | None = None,
    ) -> EvolutionResult:
        if self.environ.get("SIWAH_EVOLVE") == "off":
            return self._blocked("kill switch is active")
        if is_paused(path=self.pause_file):
            return self._blocked("evolution is paused")
        if not verify_manifest(Path(__file__).resolve().parents[3]).ok:
            return self._blocked("kernel enforcement integrity check failed")
        budget = self.governor.can_start()
        if not budget.allowed:
            self.audit("evolve.budget_blocked", {"reason": budget.reason})
            self._save_state(status="paused", reason=budget.reason)
            return EvolutionResult(status="budget_blocked", reason=budget.reason)

        started = _CycleStart(
            allowance=float(getattr(self.prober, "spent_allowance", 0.0)),
            duration=float(getattr(self.prober, "spent_duration", 0.0)),
            model_allowance=self.model_allowance(),
            monotonic=time.monotonic(),
            head=self.promoter.repo.head(),
        )
        result: EvolutionResult | None = None
        try:
            result = await self._run_cycle(observations, baseline=baseline, started=started)
            return result
        finally:
            spent, duration = self._measure_spend(started, result.probe if result else None)
            # A privacy preflight fails before any model call. Recording it would
            # consume the daily cycle and start a cooldown for work that never
            # happened, making the actionable fix impossible to retry.
            preflight_failed = (
                result is not None and result.status == "privacy_blocked" and spent <= 0.0
            )
            if preflight_failed:
                self.audit(
                    "evolve.spend_skipped",
                    {
                        "allowance": spent,
                        "duration_s": duration,
                        "reason": result.reason if result is not None else None,
                    },
                )
            else:
                # Cover reflection, proposal, rejection, probe errors,
                # cancellation, and promotion failures with one cycle entry.
                self.governor.record_cycle(spent, duration)
                self.audit("evolve.spend", {"allowance": spent, "duration_s": duration})
            if result is not None:
                result.spent_allowance = spent
                result.duration_s = duration

    async def _run_cycle(
        self,
        observations: list[Observation] | None,
        *,
        baseline: ProbeResult | None,
        started: _CycleStart,
    ) -> EvolutionResult:
        observed = observations
        if observed is None:
            if self.store is None:
                raise ValueError("observations or a Store are required")
            observed = self.observer.observe(self.store)
        signals = self.observer.signals(observed)
        self.audit("evolve.observed", {"sessions": len(observed), "signals": signals})

        try:
            reflection = await self.reflector.reflect(self.model, observed, signals=signals)
        except PrivacyError as exc:
            self.audit("evolve.privacy_blocked", {"reason": str(exc), "model": self.model})
            self._save_state(status="blocked", reason=str(exc))
            return EvolutionResult(status="privacy_blocked", reason=str(exc))
        self.audit(
            "evolve.reflected",
            {"model": reflection.model or self.model, "summary": reflection.summary},
        )

        proposed = await self.proposer.propose(reflection, observed)
        candidates = proposed if isinstance(proposed, list) else [proposed]
        if not candidates:
            self.audit("evolve.rejected", {"stage": "proposal", "errors": ["no candidate"]})
            self._save_state(status="idle", reason="no candidate")
            return EvolutionResult(status="rejected", reason="no candidate")
        candidate = candidates[0]
        for item in candidates:
            errors = validate_candidate(item, self.repo_root, model=self.model)
            if errors:
                self.audit(
                    "evolve.rejected",
                    {"candidate": item.id, "stage": "safety", "errors": errors},
                )
                self._save_state(status="idle", reason="; ".join(errors))
                return EvolutionResult(status="rejected", candidate=item, reason="; ".join(errors))

        baseline_probe = baseline
        probe: ProbeResult | None = None
        decision: GateDecision | None = None
        early_reasons: list[str] | None = None

        try:
            if len(candidates) > 1 and all(item.level > 0 for item in candidates):
                baseline_probe = baseline_probe or await self.prober.baseline(self.repo_root)
                self._audit_probe(baseline_probe, candidate.id, baseline=True)
                candidate, probe, decision = await self.composer.compose(
                    candidates, baseline_probe, str(self.repo_root)
                )
                if candidate is None or probe is None or decision is None:
                    early_reasons = (
                        decision.reasons if decision is not None else ["composition failed"]
                    )
            elif candidate.level == 0:
                probe = await self.prober.probe(candidate, self.repo_root, tier="t0")
                self._audit_probe(probe, candidate.id)
                if probe.golden.pass_rate < 1.0:
                    decision = GateDecision(
                        accepted=False,
                        reasons=["level 0 smoke probe failed"],
                        checks={"smoke": False},
                    )
                else:
                    decision = GateDecision(
                        accepted=True,
                        reasons=["level 0 validation passed"],
                        checks={"safety": True, "smoke": True},
                    )
            else:
                baseline_probe = baseline_probe or await self.prober.baseline(self.repo_root)
                self._audit_probe(baseline_probe, candidate.id, baseline=True)
                screen_baseline = await self.prober.baseline(self.repo_root, tier="t1")
                self._audit_probe(screen_baseline, candidate.id, baseline=True)
                screen = await self.prober.probe(candidate, self.repo_root, tier="t1")
                self._audit_probe(screen, candidate.id)
                if screen.golden.pass_rate < screen_baseline.golden.pass_rate:
                    probe = screen
                    decision = GateDecision(
                        accepted=False,
                        reasons=["t1 screening regressed"],
                        checks={"screening": False},
                    )
                else:
                    probe = await self.prober.probe(candidate, self.repo_root, tier="t2")
                    self._audit_probe(probe, candidate.id)
                    decision = evaluate_gate(baseline_probe.golden, probe.golden, self.gate)
        except Exception:
            self._save_state(status="idle", reason="probe failed")
            raise

        spent, duration = self._measure_spend(started, probe)
        limit = self.governor.check_cycle(spent, duration)
        if not limit.allowed:
            self.audit(
                "evolve.budget_exceeded",
                {"reason": limit.reason, "allowance": spent, "duration_s": duration},
            )
            self._save_state(status="paused", reason=limit.reason)
            return EvolutionResult(
                status="budget_blocked",
                reason=limit.reason,
                spent_allowance=spent,
                duration_s=duration,
            )

        if early_reasons is not None:
            self.audit(
                "evolve.rejected",
                {"stage": "composition", "reasons": early_reasons},
            )
            self._save_state(status="idle", reason="; ".join(early_reasons))
            return EvolutionResult(
                status="rejected",
                probe=probe,
                gate=decision.__dict__ if decision is not None else None,
                reason="; ".join(early_reasons),
                spent_allowance=spent,
                duration_s=duration,
            )

        assert candidate is not None and probe is not None and decision is not None
        self.audit(
            "evolve.gate",
            {
                "candidate": candidate.id,
                "accepted": decision.accepted,
                "reasons": decision.reasons,
                "checks": decision.checks,
                "delta_pass_rate": decision.delta_pass_rate,
                "cost_ratio": decision.cost_ratio,
            },
        )
        if not decision.accepted:
            self.audit(
                "evolve.rejected",
                {"candidate": candidate.id, "stage": "gate", "reasons": decision.reasons},
            )
            self._save_state(status="idle", reason="; ".join(decision.reasons))
            return EvolutionResult(
                status="rejected",
                candidate=candidate,
                probe=probe,
                gate=decision.__dict__,
                reason="; ".join(decision.reasons),
                spent_allowance=spent,
                duration_s=duration,
            )

        previous_tag = self.promoter.repo.latest_evolution_tag()
        if self.promoter.repo.head() != started.head:
            return EvolutionResult(status="rejected", reason="HEAD changed during evaluation")
        approval = _issue_approval(
            self.promoter.repo.root,
            self.promoter.repo.head(),
            candidate.model_dump_json(),
            policy=self.gate.model_dump_json(),
        )
        promotion = self.promoter.promote(candidate, approval=approval)
        self._promotion_tag = promotion.tag
        self._previous_commit = promotion.previous_commit
        self._previous_tag = previous_tag
        self._monitor = CanaryMonitor(self.canary_config, CanaryBaseline())
        self._save_state(
            status="canary",
            tag=promotion.tag,
            candidate=candidate.id,
            reason=None,
            canary_previous_commit=promotion.previous_commit,
            canary_previous_tag=previous_tag,
            canary_samples=[],
        )
        return EvolutionResult(
            status="promoted",
            candidate=candidate,
            probe=probe,
            gate=decision.__dict__,
            promotion=promotion,
            spent_allowance=spent,
            duration_s=duration,
        )

    def _measure_spend(
        self,
        started: _CycleStart,
        probe: ProbeResult | None,
    ) -> tuple[float, float]:
        spent_after = float(getattr(self.prober, "spent_allowance", 0.0))
        duration_after = float(getattr(self.prober, "spent_duration", 0.0))
        spent = max(0.0, spent_after - started.allowance) or (
            probe.golden.allowance if probe is not None else 0.0
        )
        spent += max(0.0, self.model_allowance() - started.model_allowance)
        duration = max(0.0, duration_after - started.duration, time.monotonic() - started.monotonic)
        return spent, duration

    def _audit_probe(
        self, probe: ProbeResult, candidate_id: str, *, baseline: bool = False
    ) -> None:
        self.audit(
            "evolve.probe",
            {
                "tier": probe.tier,
                "candidate": candidate_id,
                "baseline": baseline,
                "pass_rate": probe.golden.pass_rate,
                "tasks": probe.golden.tasks,
                "allowance": probe.golden.allowance,
                "duration_s": probe.golden.duration_s,
            },
        )

    def _resume_canary(self) -> CanaryMonitor | None:
        state = self._load_state()
        if state.status != "canary" or not state.canary_tag:
            return None
        monitor = CanaryMonitor(self.canary_config, CanaryBaseline())
        for item in state.canary_samples:
            try:
                monitor.record(CanarySample.model_validate(item))
            except ValueError:
                continue
        self._monitor = monitor
        self._promotion_tag = state.canary_tag
        self._previous_commit = state.canary_previous_commit or ""
        self._previous_tag = state.canary_previous_tag
        return monitor

    def record_canary(self, sample: CanarySample) -> CanaryDecision:
        monitor = self._monitor or self._resume_canary()
        if monitor is None:
            raise RuntimeError("no active canary; promote a candidate first")
        monitor.record(sample)
        if not monitor.ready:
            # Samples must survive the process that promoted the candidate.
            self._save_state(
                status="canary",
                tag=self._promotion_tag,
                canary_samples=[item.model_dump(mode="json") for item in monitor.samples],
            )
            return CanaryDecision(
                ok=True,
                reasons=[f"waiting for {self.canary_config.sessions} sessions"],
                sessions=len(monitor.samples),
            )
        decision = monitor.evaluate()
        self.audit(
            "evolve.canary",
            {
                "ok": decision.ok,
                "reasons": decision.reasons,
                "sessions": decision.sessions,
                "tag": self._promotion_tag,
            },
        )
        if not decision.ok and self._promotion_tag:
            target = self._previous_commit or self._promotion_tag
            self.promoter.rollback(target, reason="; ".join(decision.reasons))
            self._save_state(
                status="rolled_back",
                tag=self._previous_tag,
                reason="; ".join(decision.reasons),
                clear_canary=True,
            )
            self._monitor = None
        else:
            self._save_state(status="idle", tag=self._promotion_tag, clear_canary=True)
            self._monitor = None
        return decision

    def _blocked(self, reason: str) -> EvolutionResult:
        self.audit("evolve.blocked", {"reason": reason})
        self._save_state(status="paused", reason=reason)
        return EvolutionResult(status="blocked", reason=reason)

    def _save_state(
        self,
        *,
        status: str,
        tag: str | None = None,
        candidate: str | None = None,
        reason: str | None = None,
        canary_previous_commit: str | None = None,
        canary_previous_tag: str | None = None,
        canary_samples: list[dict[str, Any]] | None = None,
        clear_canary: bool = False,
    ) -> None:
        previous = self._load_state()
        state = EvolutionState(
            tag=tag if tag is not None else (None if status == "rolled_back" else previous.tag),
            status=status,
            last_candidate=candidate or previous.last_candidate,
            last_gate=previous.last_gate,
            updated_at=previous.updated_at,
            canary_tag=(
                None
                if clear_canary
                else (tag if status == "canary" and tag is not None else previous.canary_tag)
            ),
            canary_previous_commit=(
                None
                if clear_canary
                else (canary_previous_commit or previous.canary_previous_commit)
            ),
            canary_previous_tag=(
                None if clear_canary else (canary_previous_tag or previous.canary_previous_tag)
            ),
            canary_samples=(
                []
                if clear_canary
                else (canary_samples if canary_samples is not None else previous.canary_samples)
            ),
        )
        if reason:
            state.last_gate = {"reason": reason}
        save_state(state, path=self.state_file)

    def _load_state(self) -> EvolutionState:
        from .evolution_state import load_state

        return load_state(path=self.state_file)


__all__ = ["EvolutionEngine", "pause_path", "state_path"]
