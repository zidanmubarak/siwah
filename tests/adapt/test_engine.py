from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from siwah.adapt.canary import CanaryBaseline, CanaryMonitor
from siwah.adapt.engine import EvolutionEngine
from siwah.adapt.models import CanarySample, Candidate, Reflection
from siwah.adapt.prober import SyntheticCheck, SyntheticProber
from siwah.adapt.promoter import HarnessRepository, Promoter
from siwah.adapt.reflector import Reflector
from siwah.adapt.synthetic import SeedProposer, SeedReflector, seed_candidate, seed_prober
from siwah.config import BudgetConfig, CanaryConfig, GateConfig
from siwah.kernel.budget import BudgetGovernor
from siwah.providers.profiles import ModelProfile, PrivacyProfile


class StaticProposer:
    def __init__(self, candidate: Candidate) -> None:
        self.candidate = candidate

    async def propose(self, reflection: Reflection, observations: list[Any]) -> Candidate:
        return self.candidate


def _harness(tmp_path: Path, *, good: bool = False) -> tuple[Path, HarnessRepository]:
    root = tmp_path / "harness"
    (root / "prompts").mkdir(parents=True)
    (root / "tool_guidance").mkdir(parents=True)
    prompt = "You are siwah. Read files before editing.\n"
    if good:
        prompt += "Run the verification command and report the result.\n"
    (root / "prompts" / "system.md").write_text(prompt, encoding="utf-8")
    (root / "tool_guidance" / "01-workflow.md").write_text(
        "Use read_file before editing. Run tests.\n", encoding="utf-8"
    )
    repo = HarnessRepository(root)
    repo.ensure()
    return root, repo


def _audit(events: list[tuple[str, dict[str, Any]]]):
    def write(event: str, data: dict[str, Any]) -> dict[str, Any]:
        events.append((event, data))
        return {"event": event, "data": data}

    return write


def _engine(
    root: Path,
    repo: HarnessRepository,
    proposer: Any,
    prober: SyntheticProber,
    events: list[tuple[str, dict[str, Any]]],
    *,
    governor: BudgetGovernor | None = None,
    reflector: Any | None = None,
    model: str = "seed/local",
    gate: GateConfig | None = None,
    canary: CanaryConfig | None = None,
    tmp_path: Path | None = None,
) -> EvolutionEngine:
    audit = _audit(events)
    return EvolutionEngine(
        root,
        reflector=reflector or SeedReflector(),
        proposer=proposer,
        prober=prober,
        promoter=Promoter(repo, audit=audit),
        governor=governor
        or BudgetGovernor(
            BudgetConfig(max_cycles_per_day=3, cooldown_minutes=0),
            state_path=(tmp_path or root.parent) / "budget.json",
        ),
        gate=gate or GateConfig(),
        canary=canary or CanaryConfig(sessions=2, max_error_rate_increase=0.1),
        model=model,
        audit=audit,
        state_file=(tmp_path or root.parent) / "evolution.json",
        pause_file=(tmp_path or root.parent) / "evolution.paused",
    )


@pytest.mark.asyncio
async def test_bad_prompt_is_improved_and_promoted(tmp_path: Path):
    root, repo = _harness(tmp_path, good=False)
    candidate = seed_candidate(root)
    assert candidate is not None
    events: list[tuple[str, dict[str, Any]]] = []
    engine = _engine(
        root,
        repo,
        SeedProposer(str(root)),
        seed_prober(),
        events,
        tmp_path=tmp_path,
    )

    result = await engine.run_once([])

    assert result.status == "promoted"
    assert result.promotion is not None and result.promotion.tag == "evo-1"
    assert "verification command" in (root / "prompts" / "system.md").read_text(encoding="utf-8")
    names = [event for event, _ in events]
    assert "evolve.observed" in names
    assert "evolve.reflected" in names
    assert "evolve.gate" in names
    assert "evolve.promoted" in names


@pytest.mark.asyncio
async def test_bad_candidate_is_rejected_by_the_gate(tmp_path: Path):
    root, repo = _harness(tmp_path, good=True)
    path = root / "prompts" / "system.md"
    prompt = path.read_text(encoding="utf-8")
    candidate = Candidate(
        id="bad-remove-verification",
        level=1,
        kind="system_prompt",
        scope="global",
        target_paths=["prompts/system.md"],
        changes={"prompts/system.md": prompt.replace("verification command", "summary")},
        rationale="bad candidate",
        expected_metric="pass_rate_up",
        risk="regression",
    )
    events: list[tuple[str, dict[str, Any]]] = []
    engine = _engine(
        root,
        repo,
        StaticProposer(candidate),
        seed_prober(),
        events,
        tmp_path=tmp_path,
    )

    result = await engine.run_once([])

    assert result.status == "rejected"
    assert "regressed" in (result.reason or "")
    assert path.read_text(encoding="utf-8") == prompt
    assert repo.latest_evolution_tag() is None
    assert any(event == "evolve.rejected" for event, _ in events)


@pytest.mark.asyncio
async def test_canary_regression_rolls_back(tmp_path: Path):
    root, repo = _harness(tmp_path, good=False)
    events: list[tuple[str, dict[str, Any]]] = []
    engine = _engine(
        root,
        repo,
        SeedProposer(str(root)),
        seed_prober(),
        events,
        tmp_path=tmp_path,
    )
    result = await engine.run_once([])
    assert result.status == "promoted"
    assert "verification command" in (root / "prompts" / "system.md").read_text(encoding="utf-8")

    first = engine.record_canary(CanarySample(session_id="s1", tool_error_rate=0.5))
    assert first.ok is True
    second = engine.record_canary(CanarySample(session_id="s2", tool_error_rate=0.5))
    assert second.ok is False

    assert "verification command" not in (root / "prompts" / "system.md").read_text(
        encoding="utf-8"
    )
    names = [event for event, _ in events]
    assert "evolve.canary" in names
    assert "evolve.rollback" in names


@pytest.mark.asyncio
async def test_budget_reserve_blocks_the_cycle_before_proposing(tmp_path: Path):
    root, repo = _harness(tmp_path, good=False)
    now = datetime.now(UTC)
    governor = BudgetGovernor(
        BudgetConfig(
            max_allowance_per_cycle=0.5,
            max_cycles_per_day=3,
            reserve_weekly_pct=40,
            cooldown_minutes=0,
        ),
        state_path=tmp_path / "budget.json",
        weekly_allowance=1.0,
    )
    governor.record_cycle(0.7, 30.0, now=now)
    events: list[tuple[str, dict[str, Any]]] = []
    called = False

    class Proposer:
        async def propose(self, reflection, observations):
            nonlocal called
            called = True
            raise AssertionError("proposer must not run when the reserve is low")

    engine = _engine(
        root,
        repo,
        Proposer(),
        seed_prober(),
        events,
        governor=governor,
        tmp_path=tmp_path,
    )
    result = await engine.run_once([])
    assert result.status == "budget_blocked"
    assert not called
    assert any(event == "evolve.budget_blocked" for event, _ in events)


@pytest.mark.asyncio
async def test_training_model_is_not_used_for_reflection(tmp_path: Path):
    root, repo = _harness(tmp_path, good=False)
    calls: list[str] = []
    profile = ModelProfile(
        provider="mock",
        model="model-a",
        api="openai_chat",
        privacy=PrivacyProfile(training=True),
    )

    async def call(prompt: str) -> str:
        calls.append(prompt)
        return "should not run"

    reflector = Reflector(call, profile_loader=lambda _provider, _model: profile)
    events: list[tuple[str, dict[str, Any]]] = []
    engine = _engine(
        root,
        repo,
        SeedProposer(str(root)),
        seed_prober(),
        events,
        reflector=reflector,
        model="mock/model-a",
        tmp_path=tmp_path,
    )

    result = await engine.run_once([])

    assert result.status == "privacy_blocked"
    assert calls == []
    assert any(event == "evolve.privacy_blocked" for event, _ in events)
    budget = engine.governor.load_state()
    assert budget.cycles_today == 0
    assert budget.last_cycle_at is None
    assert any(event == "evolve.spend_skipped" for event, _ in events)


@pytest.mark.asyncio
async def test_model_specific_candidate_does_not_touch_other_models(tmp_path: Path):
    root = tmp_path / "harness"
    (root / "prompts").mkdir(parents=True)
    (root / "tool_guidance").mkdir(parents=True)
    (root / "profiles" / "mock__model-a").mkdir(parents=True)
    (root / "profiles" / "mock__model-b").mkdir(parents=True)
    (root / "prompts" / "system.md").write_text("base\n", encoding="utf-8")
    (root / "tool_guidance" / "01-workflow.md").write_text(
        "Use read_file. Run tests.\n", encoding="utf-8"
    )
    (root / "profiles" / "mock__model-a" / "system_append.md").write_text(
        "base-a\n", encoding="utf-8"
    )
    (root / "profiles" / "mock__model-b" / "system_append.md").write_text(
        "base-b\n", encoding="utf-8"
    )
    repo = HarnessRepository(root)
    repo.ensure()
    candidate = Candidate(
        id="model-a-overlay",
        level=1,
        kind="profile_overlay",
        scope="model",
        model="mock/model-a",
        target_paths=["profiles/mock__model-a/system_append.md"],
        changes={"profiles/mock__model-a/system_append.md": "Use the special local convention.\n"},
        rationale="model-specific guidance",
        expected_metric="pass_rate_up",
        risk="only model-a",
    )
    prober = SyntheticProber(
        [
            SyntheticCheck(
                name="model-a",
                path="profiles/mock__model-a/system_append.md",
                required=("special",),
            )
        ]
    )
    events: list[tuple[str, dict[str, Any]]] = []
    engine = _engine(
        root,
        repo,
        StaticProposer(candidate),
        prober,
        events,
        model="mock/model-a",
        tmp_path=tmp_path,
    )

    result = await engine.run_once([])

    assert result.status == "promoted"
    assert "special" in (root / "profiles" / "mock__model-a" / "system_append.md").read_text(
        encoding="utf-8"
    )
    assert (root / "profiles" / "mock__model-b" / "system_append.md").read_text(
        encoding="utf-8"
    ) == "base-b\n"


def test_canary_monitor_thresholds():
    monitor = CanaryMonitor(
        CanaryConfig(sessions=2, max_error_rate_increase=0.1),
        CanaryBaseline(tool_error_rate=0.05),
    )
    monitor.record(CanarySample(session_id="s1", tool_error_rate=0.1))
    monitor.record(CanarySample(session_id="s2", tool_error_rate=0.1))
    assert monitor.ready
    assert monitor.evaluate().ok
