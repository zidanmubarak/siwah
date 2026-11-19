from __future__ import annotations

from siwah.adapt.composer import non_conflicting
from siwah.adapt.models import Candidate


def _candidate(name: str, path: str) -> Candidate:
    return Candidate(
        id=name,
        level=1,
        kind="prompt",
        scope="global",
        target_paths=[path],
        changes={path: "new\n"},
        rationale="test",
        expected_metric="pass_rate_up",
        risk="low",
    )


def test_non_conflicting_drops_overlapping_paths_and_other_scopes():
    first = _candidate("a", "prompts/system.md")
    overlapping = _candidate("b", "prompts/system.md")
    other = _candidate("c", "tool_guidance/01.md")
    other.scope = "model"
    other.model = "mock/model-a"

    selected = non_conflicting([first, overlapping, other])

    assert [candidate.id for candidate in selected] == ["a"]
