from __future__ import annotations

from pathlib import Path

import pytest

from siwah.auth import AuthError, AuthStore
from siwah.config import (
    AuthConfig,
    Config,
    ConfigError,
    ModelRef,
    PermissionRule,
    PermissionsConfig,
    load_config,
    update_agent_effort,
)
from siwah.permissions.policy import (
    Approval,
    PathGuardError,
    PermissionPolicy,
    PermissionRequest,
)


def test_update_agent_effort_preserves_other_settings(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(
        """
[agent]
max_steps = 7

[budget]
max_cycles_per_day = 4
""".strip()
        + "\n",
        encoding="utf-8",
    )

    update_agent_effort("max", path)

    config = load_config(path)
    assert config.agent.effort == "max"
    assert config.agent.max_steps == 7
    assert config.agent.interactive_max_steps == 0
    assert config.budget.max_cycles_per_day == 4

    update_agent_effort("off", path)
    assert load_config(path).agent.effort is None
    with pytest.raises(ValueError):
        update_agent_effort("ultra", path)


def test_model_ref_round_trip():
    ref = ModelRef.parse("opencode-go/model-a")
    assert ref.provider == "opencode-go"
    assert ref.model == "model-a"
    assert str(ref) == "opencode-go/model-a"
    with pytest.raises(ValueError):
        ModelRef.parse("not-a-ref")


def test_load_config_from_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "config.toml"
    path.write_text(
        """
[providers.local]
api = "openai_chat"
base_url = "http://127.0.0.1:11434/v1"
auth = { scheme = "none" }

[roles]
main = "local/model-a"
fallback = ["local/model-b"]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("SIWAH_CONFIG", str(path))
    config = load_config()
    assert config.roles.main == "local/model-a"
    assert config.model_for_role("main").model == "model-a"
    assert len(config.fallback_refs()) == 1


def test_missing_role_is_actionable():
    config = Config()
    with pytest.raises(ConfigError, match="roles"):
        config.model_for_role("main")


def test_env_key_precedes_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = AuthStore(tmp_path / "auth.json")
    store.save_key("mock", "file-key")
    monkeypatch.setenv("MOCK_KEY", "env-key")
    auth = AuthConfig(scheme="bearer", env="MOCK_KEY")
    assert store.get_key("mock", auth) == "env-key"
    monkeypatch.delenv("MOCK_KEY")
    assert store.get_key("mock", auth) == "file-key"


def test_auth_file_saves_and_deletes(tmp_path: Path):
    store = AuthStore(tmp_path / "auth.json")
    path = store.save_key("mock", "secret")
    assert path.exists()
    assert store.get_key("mock", AuthConfig()) == "secret"
    assert store.delete_key("mock") is True
    assert store.get_key("mock", AuthConfig()) is None
    with pytest.raises(AuthError):
        store.require_key("missing", AuthConfig())


def test_permission_path_guard(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("TOKEN=secret\n")
    (workspace / "src" / "siwah" / "kernel").mkdir(parents=True)
    policy = PermissionPolicy(workspace, PermissionsConfig())
    assert policy.resolve("README.md").name == "README.md"
    with pytest.raises(PathGuardError):
        policy.resolve("../outside.txt")
    with pytest.raises(PathGuardError):
        policy.resolve(".env")
    with pytest.raises(PathGuardError):
        policy.resolve("src/siwah/kernel/guard.py")
    assert policy.check(PermissionRequest(tool="read_file", kind="read", preview="x")) == "allow"
    assert policy.check(PermissionRequest(tool="write_file", kind="write", preview="x")) == "ask"


def test_permission_rules_and_session_approval(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = PermissionsConfig(
        rules=[
            PermissionRule(tool="bash", command="pytest*", decision="allow"),
            PermissionRule(tool="web_fetch", decision="deny"),
        ]
    )
    policy = PermissionPolicy(workspace, config)
    assert (
        policy.check(PermissionRequest(tool="bash", kind="bash", command="pytest -q", preview="x"))
        == "allow"
    )
    assert policy.check(PermissionRequest(tool="web_fetch", kind="network", preview="x")) == "deny"
    request = PermissionRequest(tool="bash", kind="bash", command="ls", preview="x")
    assert policy.check(request) == "ask"
    policy.approve(request, Approval(decision="session"))
    assert policy.check(request) == "allow"

    assert (
        policy.check(
            PermissionRequest(tool="bash", kind="bash", command="rm -rf build", preview="x")
        )
        == "ask"
    )
    other = PermissionRequest(tool="bash", kind="bash", command="pwd", preview="x")
    assert policy.check(other) == "ask"
    policy.approve(other, Approval(decision="always"))
    assert policy.check(other) == "allow"
    assert (
        policy.check(PermissionRequest(tool="bash", kind="bash", command="echo hi", preview="x"))
        == "allow"
    )
    assert (
        policy.check(
            PermissionRequest(tool="bash", kind="bash", command="rm -rf build", preview="x")
        )
        == "allow"
    )


def test_yolo_and_destructive_commands(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = PermissionPolicy(workspace, PermissionsConfig(mode="yolo"))
    assert (
        policy.check(PermissionRequest(tool="bash", kind="bash", command="ls", preview="x"))
        == "allow"
    )
    destructive = PermissionRequest(tool="bash", kind="bash", command="rm -rf build", preview="x")
    assert policy.check(destructive) == "allow"
    catastrophic = PermissionRequest(tool="bash", kind="bash", command="rm -rf /", preview="x")
    assert policy.check(catastrophic) == "deny"


def test_explicit_allow_rule_can_approve_destructive_command(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = PermissionPolicy(
        workspace,
        PermissionsConfig(
            mode="ask",
            rules=[
                PermissionRule(tool="bash", command="rm -rf build*", decision="allow"),
            ],
        ),
    )
    request = PermissionRequest(tool="bash", kind="bash", command="rm -rf build", preview="x")
    assert policy.check(request) == "allow"
    catastrophic = PermissionRequest(tool="bash", kind="bash", command="rm -rf /", preview="x")
    assert policy.check(catastrophic) == "deny"


def test_first_run_creates_default_provider_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SIWAH_CONFIG", raising=False)
    config = load_config()
    assert set(config.providers) == {"opencode-go", "openai"}
    assert config.roles.main is None
    assert (tmp_path / "home" / "harness" / "config.toml").exists()
