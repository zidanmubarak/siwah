from __future__ import annotations

import asyncio
from pathlib import Path

from siwah.providers.profiles import load_profile, probe_model, profile_path, save_profile

from .conftest import ScriptedAdapter, outcome, tool_call


def test_probe_and_profile_round_trip(config, tmp_path: Path):
    adapter = ScriptedAdapter(
        [
            outcome(tool_calls=[tool_call("probe_echo", {"value": "ok"})]),
            outcome(
                tool_calls=[
                    tool_call("probe_echo", {"value": "a"}, "call_a"),
                    tool_call("probe_echo", {"value": "b"}, "call_b"),
                ]
            ),
            outcome(text='{"ok":true}'),
        ],
        config.providers["mock"],
    )
    info = {
        "context_window": 100_000,
        "max_output": 8_000,
        "reasoning_field": "reasoning_content",
        "privacy": {"training": False, "retention_days": 0},
        "pricing": {"input": 1},
    }
    profile = asyncio.run(probe_model(adapter, "mock", "model-a", model_info=info))
    assert profile.supports_tools is True
    assert profile.parallel_tool_calls is True
    assert profile.reasoning_field == "reasoning_content"
    assert profile.context_window == 100_000
    assert profile.privacy.training is False
    assert profile.quirks.json_compliance is True
    assert len(adapter.requests) == 3

    path = save_profile(profile, root=tmp_path / "profiles")
    assert path.exists()
    assert profile_path("mock", "model-a", root=tmp_path / "profiles") == path
    loaded = load_profile("mock", "model-a", root=tmp_path / "profiles")
    assert loaded.model == "model-a"
    assert loaded.supports_tools is True
