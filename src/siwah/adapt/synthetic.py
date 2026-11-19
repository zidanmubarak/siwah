"""Cold-start synthetic evolution scenario."""

from __future__ import annotations

from pathlib import Path

from .models import Candidate, Observation, Reflection
from .prober import SyntheticCheck, SyntheticProber
from .proposer import ProposalError

SEED_CHECKS = [
    SyntheticCheck(
        name="system-verification",
        path="prompts/system.md",
        required=("verification command",),
    ),
    SyntheticCheck(
        name="tool-workflow",
        path="tool_guidance/01-workflow.md",
        required=("read_file", "tests"),
    ),
]


def seed_prober() -> SyntheticProber:
    return SyntheticProber(SEED_CHECKS, t1_size=1)


def seed_candidate(harness_root: Path | str) -> Candidate | None:
    root = Path(harness_root)
    prompt_path = root / "prompts" / "system.md"
    try:
        prompt = prompt_path.read_text(encoding="utf-8")
    except OSError:
        return None
    if "verification command" in prompt.lower():
        return None
    addition = (
        "\n- Before finalizing, run the project's verification command and report "
        "the result. If no command is available, state what was checked.\n"
    )
    return Candidate(
        id="seed-verification-guidance",
        level=1,
        kind="system_prompt",
        scope="global",
        target_paths=["prompts/system.md"],
        changes={"prompts/system.md": prompt.rstrip() + "\n" + addition},
        rationale="The sessions show a gap between edits and explicit verification evidence.",
        evidence=["synthetic cold-start check: verification command guidance missing"],
        expected_metric="pass_rate_up",
        risk="Adds one concise instruction; no permissions or guardrails change.",
    )


class SeedReflector:
    async def reflect(
        self,
        model: str,
        observations: list[Observation],
        *,
        signals: list[str] | None = None,
    ) -> Reflection:
        return Reflection(
            summary="Cold-start synthetic check: verification guidance is missing.",
            signals=list(signals or ["synthetic cold start"]),
            model=model,
        )


class SeedProposer:
    def __init__(self, harness_root: str) -> None:
        self.harness_root = harness_root

    async def propose(self, reflection: Reflection, observations: list[Observation]) -> Candidate:
        candidate = seed_candidate(self.harness_root)
        if candidate is None:
            raise ProposalError("the seed candidate is already present")
        return candidate


__all__ = [
    "SEED_CHECKS",
    "SeedProposer",
    "SeedReflector",
    "seed_candidate",
    "seed_prober",
]
