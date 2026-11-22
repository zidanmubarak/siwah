from __future__ import annotations

import asyncio
from pathlib import Path

from typer.testing import CliRunner

from siwah.cli.main import app
from siwah.providers.profiles import (
    ModelProfile,
    PrivacyProfile,
    load_profile,
    merge_privacy,
    probe_model,
    profile_path,
    save_profile,
    set_privacy,
)

from .conftest import ScriptedAdapter, outcome, tool_call


def test_merge_privacy_preserves_a_human_assertion():
    previous = ModelProfile(
        provider="mock",
        model="model-a",
        api="openai_chat",
        privacy=PrivacyProfile(training=False, retention_days=7),
    )
    fresh = ModelProfile(provider="mock", model="model-a", api="openai_chat")

    merged = merge_privacy(fresh, previous)

    assert merged.privacy.training is False
    assert merged.privacy.retention_days == 7


def test_set_privacy_round_trips(tmp_path: Path):
    profile = ModelProfile(provider="mock", model="model-a", api="openai_chat")
    save_profile(profile, root=tmp_path)

    updated, path = set_privacy("mock", "model-a", training=False, retention_days=0, root=tmp_path)

    assert updated.privacy.training is False
    assert path.exists()
    loaded = load_profile("mock", "model-a", root=tmp_path)
    assert loaded.privacy.training is False
    assert loaded.privacy.retention_days == 0


def test_models_privacy_cli_updates_the_profile(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[providers.mock]\napi = "openai_chat"\n'
        'base_url = "http://fixture.invalid/v1"\n'
        'models_url = "http://fixture.invalid/v1/models"\n'
        'auth = { scheme = "none" }\n\n[roles]\nmain = "mock/model-a"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("SIWAH_CONFIG", str(config_file))
    save_profile(ModelProfile(provider="mock", model="model-a", api="openai_chat"))

    result = CliRunner().invoke(app, ["models", "privacy", "mock/model-a", "--training", "false"])

    assert result.exit_code == 0, result.output
    assert "training=false" in result.output
    loaded = load_profile("mock", "model-a")
    assert loaded.privacy.training is False


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
