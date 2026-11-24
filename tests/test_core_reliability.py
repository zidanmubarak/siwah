"""Regression coverage for the core reliability audit (synthetic, offline)."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from siwah.config import Config, PermissionRule, PermissionsConfig, VerifyConfig
from siwah.core.checkpoints import CheckpointManager
from siwah.core.storage import Store
from siwah.core.subagents import SubagentManager, SubagentResult
from siwah.core.types import Message
from siwah.core.verify import Verifier, VerifyCommandResult, VerifyReport
from siwah.harness.bootstrap import ensure_harness
from siwah.paths import harness_dir, trajectories_dir
from siwah.permissions.policy import Approval, PermissionPolicy
from siwah.tools.base import ToolContext
from siwah.tools.task import TaskTool

from .conftest import ScriptedAdapter, make_loop, outcome, tool_call


async def _emit(_event: str, _data: dict[str, Any]) -> None:
    return None


async def _approve(_request, _request_id) -> Approval:
    return Approval(decision="once")


def _verify_config(config: Config, *, mode: str = "ask") -> Config:
    return config.model_copy(
        update={
            "permissions": PermissionsConfig(mode=mode),  # type: ignore[arg-type]
            "verify": VerifyConfig(auto=True, retry_flaky=False, max_repair_steps=0),
        }
    )


def _verifier_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "value.txt").write_text("old", encoding="utf-8")
    (repo / "check.py").write_text(
        "from pathlib import Path\n"
        "Path('verifier-ran').write_text('yes')\n"
        "value = Path('value.txt').read_text()\n"
        "print('OLD_FAILURE' if value == 'old' else 'NEW_FAILURE')\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    python = Path(sys.executable).as_posix()
    (repo / "verify.toml").write_text(
        f"[commands]\ntest = '\"{python}\" check.py'\n", encoding="utf-8"
    )
    return repo


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["no_approver", "hook", "policy"])
async def test_rejected_edit_never_runs_baseline(
    config, store: Store, tmp_path: Path, monkeypatch, denial: str
):
    cfg = _verify_config(config, mode="ask" if denial == "no_approver" else "yolo")
    if denial == "policy":
        cfg.permissions.rules.append(PermissionRule(tool="write_file", decision="deny"))
    if denial == "hook":
        from siwah.hooks import HookResult

        class DenyHook:
            async def run(self, event: str, _payload: dict):
                return HookResult(allow=False) if event == "PreToolUse" else HookResult()

        monkeypatch.setattr("siwah.core.loop.HookManager.load", lambda: DenyHook())
    repo = _verifier_repo(tmp_path)
    adapter = ScriptedAdapter(
        [
            outcome(tool_calls=[tool_call("write_file", {"path": "target.txt", "content": "x"})]),
            outcome(text="No changes."),
        ],
        cfg.providers["mock"],
    )
    loop = make_loop(cfg, store, adapter)
    session = store.create_session(cwd=str(repo), model="mock/model-a")
    await loop.run(session, "write", emit=_emit)
    assert not (repo / "target.txt").exists()
    assert not (repo / "verifier-ran").exists()
    await loop.close_jobs()


@pytest.mark.asyncio
async def test_verifier_requires_separate_shell_approval(config, store: Store, tmp_path: Path):
    cfg = _verify_config(config)
    repo = _verifier_repo(tmp_path)
    requests = []

    async def resolver(request, _request_id):
        requests.append(request)
        return Approval(decision="deny" if request.kind == "bash" else "once")

    adapter = ScriptedAdapter(
        [
            outcome(tool_calls=[tool_call("write_file", {"path": "value.txt", "content": "new"})]),
            outcome(text="All tests pass. Done."),
        ],
        cfg.providers["mock"],
    )
    loop = make_loop(cfg, store, adapter)
    session = store.create_session(cwd=str(repo), model="mock/model-a")
    result = await loop.run(session, "write", emit=_emit, permission_resolver=resolver)
    assert requests[0].tool == "write_file"
    assert any(request.tool == "bash" and request.command for request in requests)
    assert not (repo / "verifier-ran").exists()
    assert result.error == "verification_failed"
    await loop.close_jobs()


def test_changed_failure_under_same_command_is_a_regression():
    old = VerifyCommandResult(name="test", command="pytest", ok=False, exit_code=1, output="OLD")
    new = old.model_copy(update={"output": "NEW"})
    baseline = VerifyReport(status="failed", commands=[old])
    report = VerifyReport(status="failed", commands=[new])
    assert report.new_failures(baseline) == [new]
    assert baseline.new_failures(baseline) == []


@pytest.mark.asyncio
async def test_verifier_fingerprints_changes_outside_the_retained_output(config, tmp_path):
    repo = _verifier_repo(tmp_path)
    (repo / "check.py").write_text(
        "from pathlib import Path\n"
        "print(Path('value.txt').read_text())\n"
        "print('identical tail' * 100)\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    context = ToolContext(
        workspace=repo,
        policy=PermissionPolicy(repo, PermissionsConfig(mode="yolo")),
        session_id="test",
        agent_config=config.agent,
    )
    verifier = Verifier(
        repo,
        VerifyConfig(auto=True, retry_flaky=False, keep_output_chars=50),
        authorize=context.authorize,
    )
    baseline = await verifier.run()
    (repo / "value.txt").write_text("new", encoding="utf-8")
    changed = await verifier.run()
    assert baseline.commands[0].output == changed.commands[0].output
    assert changed.new_failures(baseline) == changed.commands


@pytest.mark.asyncio
async def test_verifier_without_permission_bridge_fails_closed(tmp_path):
    repo = _verifier_repo(tmp_path)
    report = await Verifier(repo, VerifyConfig(auto=True)).run()
    assert report.status == "failed"
    assert report.commands[0].denied
    assert not (repo / "verifier-ran").exists()


@pytest.mark.asyncio
async def test_verifier_uses_configured_sandbox(config, store, tmp_path, monkeypatch):
    from siwah.sandbox import SandboxResult

    calls = []

    async def unavailable(self, command, **_kwargs):
        calls.append(command)
        return SandboxResult(
            False, "sandbox backend is unavailable: missing-docker", 1, "missing-docker"
        )

    monkeypatch.setattr("siwah.sandbox.Sandbox.shell", unavailable)
    cfg = _verify_config(config, mode="yolo")
    cfg = cfg.model_copy(update={"sandbox": cfg.sandbox.model_copy(update={"enabled": True})})
    repo = _verifier_repo(tmp_path)
    adapter = ScriptedAdapter(
        [
            outcome(tool_calls=[tool_call("write_file", {"path": "value.txt", "content": "new"})]),
            outcome(text="All tests pass."),
        ],
        cfg.providers["mock"],
    )
    loop = make_loop(cfg, store, adapter)
    session = store.create_session(cwd=str(repo), model="mock/model-a")
    result = await loop.run(session, "change", emit=_emit)
    assert result.error == "verification_failed"
    assert len(calls) == 2
    assert not (repo / "verifier-ran").exists()
    await loop.close_jobs()


@pytest.mark.asyncio
async def test_verifier_cancellation_terminates_process_and_drains_reader(
    config, tmp_path, monkeypatch
):
    started = asyncio.Event()
    stopped = asyncio.Event()

    class Process:
        returncode = None

        async def communicate(self):
            started.set()
            await stopped.wait()
            return b"partial output", None

    process = Process()
    terminated = []

    async def spawn(*_args, **_kwargs):
        return process

    async def terminate(target):
        terminated.append(target)
        target.returncode = -1
        stopped.set()

    monkeypatch.setattr("siwah.core.verify.asyncio.create_subprocess_shell", spawn)
    monkeypatch.setattr("siwah.core.verify.terminate_process_tree", terminate)
    context = ToolContext(
        workspace=tmp_path,
        policy=PermissionPolicy(tmp_path, PermissionsConfig(mode="yolo")),
        session_id="test",
        agent_config=config.agent,
    )
    verifier = Verifier(
        tmp_path,
        VerifyConfig(commands={"test": "synthetic-command"}),
        authorize=context.authorize,
    )
    task = asyncio.create_task(verifier.run())
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert terminated == [process]
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_changed_failure_cannot_accept_unexplained_success(config, store, tmp_path):
    cfg = _verify_config(config, mode="yolo")
    repo = _verifier_repo(tmp_path)
    events = []

    async def emit(event: str, data: dict):
        events.append((event, data))

    adapter = ScriptedAdapter(
        [
            outcome(tool_calls=[tool_call("write_file", {"path": "value.txt", "content": "new"})]),
            outcome(text="All tests pass. Done."),
        ],
        cfg.providers["mock"],
    )
    loop = make_loop(cfg, store, adapter)
    session = store.create_session(cwd=str(repo), model="mock/model-a")
    result = await loop.run(session, "change", emit=emit)
    assert result.error == "verification_failed"
    assert any(e == "verifier" and d.get("new_failures") == ["test"] for e, d in events)
    await loop.close_jobs()


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_checkpoint_installs_exclusions_on_fresh_git_and_preserves_custom_rules(
    store: Store, tmp_path: Path
):
    repo = tmp_path / "project"
    repo.mkdir()
    for name in (".siwah", ".venv"):
        (repo / name).mkdir()
        (repo / name / "private.txt").write_text("not captured", encoding="utf-8")
    (repo / "main.txt").write_text("source", encoding="utf-8")
    (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (repo / "ignored.txt").write_text("ignored", encoding="utf-8")
    session = store.create_session(cwd=str(repo), model=None)
    manager = CheckpointManager(repo, session.id, store=store)
    assert manager.ensure()
    exclude = manager.git_dir / "info" / "exclude"
    exclude.write_text(exclude.read_text() + "\ncustom.txt\n", encoding="utf-8")
    (repo / "custom.txt").write_text("custom ignore", encoding="utf-8")
    resumed = CheckpointManager(repo, session.id, store=store)
    assert resumed.ensure()
    assert resumed.initial()
    files = resumed._git("ls-tree", "-r", "--name-only", "HEAD", capture=True).stdout.splitlines()
    assert "main.txt" in files
    assert not any(name.startswith((".siwah/", ".venv/")) for name in files)
    assert "ignored.txt" not in files and "custom.txt" not in files
    assert exclude.read_text().count("# Managed by siwah checkpoints.") == 1


@pytest.mark.asyncio
async def test_provider_ids_are_replaced_by_unique_safe_local_ids(config, store, sample_repo):
    repeated = str(sample_repo.parent / "untrusted-provider-id")
    adapter = ScriptedAdapter(
        [
            outcome(tool_calls=[tool_call("read_file", {"path": "README.md"}, repeated)]),
            outcome(text="first"),
            outcome(tool_calls=[tool_call("read_file", {"path": "README.md"}, repeated)]),
            outcome(text="second"),
        ],
        config.providers["mock"],
    )
    loop = make_loop(config, store, adapter)
    sessions = [store.create_session(cwd=str(sample_repo), model="mock/model-a") for _ in range(2)]
    for session in sessions:
        await loop.run(session, "read", emit=_emit)
    calls = [store.get_tool_calls(session.id) for session in sessions]
    assert all(len(items) == 1 for items in calls)
    ids = [items[0]["id"] for items in calls]
    assert len(set(ids)) == 2
    assert all(re.fullmatch(r"call_[a-f0-9]{32}", item) for item in ids)
    for session, call_id in zip(sessions, ids, strict=True):
        messages = store.get_messages(session.id)
        assert messages[1].tool_calls[0].id == call_id
        assert messages[2].tool_call_id == call_id
    await loop.close_jobs()


@pytest.mark.asyncio
async def test_repeated_provider_ids_within_one_message_are_distinct(config, store, sample_repo):
    adapter = ScriptedAdapter(
        [
            outcome(
                tool_calls=[
                    tool_call("read_file", {"path": "README.md"}, "../same:id"),
                    tool_call("read_file", {"path": "math_utils.py"}, "../same:id"),
                ]
            ),
            outcome(text="Read both."),
        ],
        config.providers["mock"],
    )
    loop = make_loop(config, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    result = await loop.run(session, "read both", emit=_emit)
    assert result.error is None
    calls = store.get_tool_calls(session.id)
    assert len(calls) == 2
    assert len({call["id"] for call in calls}) == 2
    assistant = store.get_messages(session.id)[1]
    replies = [
        message.tool_call_id for message in store.get_messages(session.id) if message.role == "tool"
    ]
    assert [call.id for call in assistant.tool_calls] == replies
    await loop.close_jobs()


def test_storage_refuses_cross_session_id_collision_without_deleting_history(store):
    first = store.create_session(cwd=".", model=None)
    second = store.create_session(cwd=".", model=None)
    options = dict(
        tool_call_id="legacy_id",
        message_id=None,
        name="read_file",
        arguments={"path": "a.txt"},
        result={"output": "original"},
        status="ok",
    )
    store.record_tool_call(session_id=first.id, **options)
    with pytest.raises(ValueError, match="already belongs"):
        store.record_tool_call(session_id=second.id, **options)
    assert store.get_tool_calls(first.id)[0]["result"]["output"] == "original"
    assert store.get_tool_calls(second.id) == []


def test_unrelated_project_agent_does_not_reset_global_override(config, store, sample_repo):
    root = sample_repo / "global-agents"
    root.mkdir()
    (root / "general.md").write_text(
        "---\nname: general\nread_only: true\ntools: read_file\nmodel: mock/locked\n---\nGlobal",
        encoding="utf-8",
    )
    project = sample_repo / ".siwah" / "subagents"
    project.mkdir(parents=True)
    (project / "other.md").write_text("---\nname: other\n---\nOther", encoding="utf-8")
    session = store.create_session(cwd=str(sample_repo), model=None)
    loop = make_loop(config, store, ScriptedAdapter([], config.providers["mock"]))
    manager = SubagentManager(
        config=config,
        store=store,
        router=loop.router,
        tools=[],
        parent_session=session,
        emit=_emit,
        root=root,
    )
    assert manager.specs["general"].read_only
    assert manager.specs["general"].model == "mock/locked"
    assert manager.specs["general"].tools == ("read_file",)
    (project / "general.md").write_text(
        "---\nname: general\nread_only: false\n---\nProject", encoding="utf-8"
    )
    manager.reload_specs()
    assert not manager.specs["general"].read_only
    assert manager.specs["general"].body == "Project"


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
async def test_task_denial_never_launches_child(config, tmp_path, batch):
    class Manager:
        calls = 0

        async def run(self, name, prompt, **_kwargs):
            self.calls += 1
            return SubagentResult(name, prompt, "child", None)

        async def run_many(self, tasks):
            self.calls += 1
            return []

    manager = Manager()
    context = ToolContext(
        workspace=tmp_path,
        policy=PermissionPolicy(
            tmp_path,
            PermissionsConfig(mode="yolo", rules=[PermissionRule(tool="task", decision="deny")]),
        ),
        session_id="parent",
        agent_config=config.agent,
        subagent_manager=manager,
    )
    arguments = {"subagent_type": "explore", "prompt": "read"}
    if batch:
        arguments = {"tasks": [arguments]}
    result = await TaskTool().run(context, **arguments)
    assert not result.ok
    assert manager.calls == 0


@pytest.mark.asyncio
async def test_child_allowance_stops_before_tools_and_next_request(config, store, sample_repo):
    cfg = config.model_copy(
        update={"subagents": config.subagents.model_copy(update={"max_allowance": 0.1})}
    )
    adapter = ScriptedAdapter(
        [
            outcome(
                tool_calls=[tool_call("write_file", {"path": "unwanted.txt", "content": "x"})],
                allowance=0.2,
            ),
            outcome(text="must not be called", allowance=0.2),
        ],
        cfg.providers["mock"],
    )
    loop = make_loop(cfg, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    manager = SubagentManager(
        config=cfg,
        store=store,
        router=loop.router,
        tools=loop.tools.tools(),
        parent_session=session,
        emit=_emit,
    )
    result = await manager.run("general", "write")
    assert len(adapter.requests) == 1
    assert not (sample_repo / "unwanted.txt").exists()
    assert result.usage.allowance == pytest.approx(0.2)
    assert result.error == "allowance_exceeded"
    child_messages = store.get_messages(result.session_id)
    assert child_messages[-1].role == "tool"  # the skipped call still has a protocol reply
    calls = store.get_tool_calls(result.session_id)
    assert [call["status"] for call in calls] == ["skipped"]
    await loop.close_jobs()


@pytest.mark.asyncio
async def test_pre_existing_failure_requires_a_candid_answer(config, store, tmp_path):
    cfg = _verify_config(config, mode="yolo")
    repo = _verifier_repo(tmp_path)
    adapter = ScriptedAdapter(
        [
            outcome(tool_calls=[tool_call("write_file", {"path": "README.md", "content": "note"})]),
            outcome(text="All tests pass. Done."),
            outcome(text="The verifier still fails on the pre-existing OLD_FAILURE."),
        ],
        cfg.providers["mock"],
    )
    loop = make_loop(cfg, store, adapter)
    session = store.create_session(cwd=str(repo), model="mock/model-a")
    result = await loop.run(session, "write", emit=_emit)
    assert result.error is None
    assert result.text == "The verifier still fails on the pre-existing OLD_FAILURE."
    messages = store.get_messages(session.id)
    assert any(
        message.role == "system" and "failed before your edits" in message.content
        for message in messages
    )
    await loop.close_jobs()


@pytest.mark.asyncio
async def test_delegated_edit_is_verified_and_checkpointed_in_parent(config, store, tmp_path):
    cfg = _verify_config(config, mode="yolo")
    cfg = cfg.model_copy(
        update={"checkpoints": cfg.checkpoints.model_copy(update={"enabled": True})}
    )
    repo = _verifier_repo(tmp_path)
    adapter = ScriptedAdapter(
        [
            outcome(
                tool_calls=[tool_call("task", {"subagent_type": "general", "prompt": "change"})]
            ),
            outcome(tool_calls=[tool_call("write_file", {"path": "value.txt", "content": "new"})]),
            outcome(text="child done"),
            outcome(text="All tests pass. Done."),
        ],
        cfg.providers["mock"],
    )
    loop = make_loop(cfg, store, adapter)
    session = store.create_session(cwd=str(repo), model="mock/model-a")
    result = await loop.run(session, "delegate", emit=_emit)
    assert result.error == "verification_failed"
    assert (repo / "verifier-ran").exists()
    if shutil.which("git"):
        labels = [record["label"] for record in store.list_checkpoints(session.id)]
        assert any(label.startswith("task:") for label in labels)
    await loop.close_jobs()


@pytest.mark.asyncio
async def test_persistence_redacts_copies_without_changing_tool_arguments(
    config, store, sample_repo
):
    secret = "sk-SYNTHETIC_RELIABILITY_SECRET_0123456789"
    arguments = {"path": "note.txt", "content": secret}
    call = tool_call("write_file", arguments)
    adapter = ScriptedAdapter(
        [outcome(tool_calls=[call]), outcome(text="Saved.")], config.providers["mock"]
    )
    loop = make_loop(config, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    await loop.run(session, f"save {secret}", emit=_emit)
    assert (sample_repo / "note.txt").read_text() == secret
    assert call.arguments["content"] == secret
    persisted = json.dumps(
        {
            "messages": [message.model_dump() for message in store.get_messages(session.id)],
            "views": store.get_message_views(session.id),
            "tools": store.get_tool_calls(session.id),
        }
    )
    assert secret not in persisted
    assert not store.search_messages("SYNTHETIC_RELIABILITY_SECRET")
    assert secret not in (trajectories_dir() / f"{session.id}.jsonl").read_text()
    await loop.close_jobs()


def test_structured_secret_values_are_redacted_in_storage(store, sample_repo):
    session = store.create_session(cwd=str(sample_repo), model=None)
    arguments = {"password": "SYNTHETIC_PASSWORD_0123456789"}
    store.record_tool_call(
        session_id=session.id,
        tool_call_id="call_local",
        message_id=None,
        name="example",
        arguments=arguments,
        result={"data": arguments},
        status="ok",
    )
    store.append_message(session.id, Message(role="user", content="plain searchable text"))
    assert "SYNTHETIC_PASSWORD" not in json.dumps(store.get_tool_calls(session.id))
    assert arguments["password"] == "SYNTHETIC_PASSWORD_0123456789"


@pytest.mark.asyncio
async def test_same_session_keeps_pinned_prompt_and_new_session_sees_update(
    config, store, sample_repo
):
    ensure_harness()
    system = harness_dir() / "prompts" / "system.md"
    system.write_text("ORIGINAL_SESSION_PROMPT", encoding="utf-8")
    adapter = ScriptedAdapter(
        [outcome(text="one"), outcome(text="two"), outcome(text="three")],
        config.providers["mock"],
    )
    loop = make_loop(config, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    await loop.run(session, "one", emit=_emit)
    system.write_text("PROMOTED_NEW_SESSION_PROMPT", encoding="utf-8")
    # Resume through another loop instance to exercise persisted pinning.
    resumed = make_loop(config, store, adapter)
    await resumed.run(session, "two", emit=_emit)
    new_session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    await resumed.run(new_session, "three", emit=_emit)
    first, second, third = [request.messages[0].content for request in adapter.requests]
    assert first == second
    assert "PROMOTED_NEW_SESSION_PROMPT" in (third or "")
    await loop.close_jobs()
    await resumed.close_jobs()
