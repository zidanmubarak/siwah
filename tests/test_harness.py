from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import pytest

from siwah.config import AgentConfig, PermissionsConfig, load_config
from siwah.core.storage import Store
from siwah.core.types import Message
from siwah.harness.bootstrap import ensure_harness
from siwah.harness.memory import (
    init_agents_file,
    init_project_memory,
    quick_note,
    read_memory,
    user_memory_path,
    write_memory,
)
from siwah.harness.prompt import build_system_prompt
from siwah.harness.skills import (
    create_skill,
    delete_skill,
    list_skills,
    read_skill,
    update_skill,
)
from siwah.paths import legacy_config_path
from siwah.permissions.policy import Approval, PermissionPolicy, PermissionRequest
from siwah.tools.base import ToolContext
from siwah.tools.harness import MemoryWriteTool, RecallTool, SkillManageTool, SkillViewTool


def use_home(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("SIWAH_HOME", str(home))
    monkeypatch.delenv("SIWAH_CONFIG", raising=False)
    return home


def make_context(workspace: Path, **kwargs) -> ToolContext:
    async def approver(_request: PermissionRequest) -> Approval:
        return Approval(decision="once")

    return ToolContext(
        workspace=workspace,
        policy=PermissionPolicy(workspace, PermissionsConfig(mode="auto-edit")),
        session_id=kwargs.pop("session_id", "ses_test"),
        agent_config=AgentConfig(),
        approver=approver,
        **kwargs,
    )


def test_harness_bootstrap_externalizes_the_prompt(tmp_path: Path, monkeypatch):
    use_home(monkeypatch, tmp_path)
    root = ensure_harness()
    assert (root / "prompts" / "system.md").exists()
    assert (root / "tool_guidance" / "01-workflow.md").exists()
    assert (root / "tool_guidance" / "03-efficiency.md").exists()
    assert (root / "skills").is_dir()
    ignore = (root / ".gitignore").read_text(encoding="utf-8")
    assert "cron.json" in ignore
    assert "cron.run.lock" in ignore
    if shutil.which("git"):
        assert (root / ".git").exists()

    (root / "prompts" / "system.md").write_text(
        "CUSTOM HARNESS\nWorkspace: {{workspace}}\n{{memory}}\n{{skills}}\n"
        "{{tool_guidance}}\n{{agents_md}}\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("Use the project test command.\n", encoding="utf-8")
    write_memory(workspace, target="user", content="Prefer short answers.")
    init_project_memory(workspace)
    create_skill("deploy", "Deploy the service safely", "Run the deploy script after tests.")
    overlay = root / "profiles" / "mock__model-a" / "system_append.md"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text("MODEL OVERLAY RULE", encoding="utf-8")

    prompt = build_system_prompt(workspace, "mock/model-a")
    assert "CUSTOM HARNESS" in prompt
    assert str(workspace) in prompt
    assert "Prefer short answers." in prompt
    assert "deploy: Deploy the service safely" in prompt
    assert "Use the project test command." in prompt
    assert "Tool workflow" in prompt
    assert "Batch independent tool calls" in prompt
    assert "Keep reasoning concise" in prompt
    assert "MODEL OVERLAY RULE" in prompt


def test_agents_context_reads_parent_chain(tmp_path: Path):
    root = tmp_path / "repo"
    nested = root / "packages" / "api"
    nested.mkdir(parents=True)
    (root / "AGENTS.md").write_text("Root guidance\n", encoding="utf-8")
    (root / "packages" / "AGENTS.md").write_text("Package guidance\n", encoding="utf-8")
    (nested / "AGENTS.md").write_text("Nested guidance\n", encoding="utf-8")

    from siwah.harness.memory import agents_context

    context = agents_context(nested)
    assert "Root guidance" in context
    assert "Package guidance" in context
    assert "Nested guidance" in context
    assert context.index("Root guidance") < context.index("Nested guidance")


def test_project_skills_override_global_skills(tmp_path: Path, monkeypatch):
    use_home(monkeypatch, tmp_path)
    workspace = tmp_path / "workspace"
    project_skill = workspace / ".siwah" / "skills" / "deploy" / "SKILL.md"
    project_skill.parent.mkdir(parents=True)
    project_skill.write_text(
        "---\nname: deploy\ndescription: Project deploy flow\n---\nProject body\n",
        encoding="utf-8",
    )
    create_skill("deploy", "Global deploy flow", "Global body")
    create_skill("global-only", "Global only", "Global only body")

    skills = list_skills(workspace)
    assert {skill.name: skill.description for skill in skills}["deploy"] == "Project deploy flow"
    assert [skill.name for skill in skills].count("deploy") == 1
    assert "global-only" in [skill.name for skill in skills]
    assert read_skill("deploy", workspace).content == "Project body"


def test_legacy_config_is_migrated(tmp_path: Path, monkeypatch):
    home = use_home(monkeypatch, tmp_path)
    legacy = legacy_config_path()
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        """
[providers.local]
api = "openai_chat"
base_url = "http://127.0.0.1:11434/v1"
auth = { scheme = "none" }

[roles]
main = "local/model-a"
""",
        encoding="utf-8",
    )
    config = load_config()
    assert config.roles.main == "local/model-a"
    migrated = home / "harness" / "config.toml"
    assert migrated.exists()
    assert "providers.local" in migrated.read_text(encoding="utf-8")
    assert legacy.exists()


def test_skills_are_listed_viewed_updated_and_deleted(tmp_path: Path, monkeypatch):
    use_home(monkeypatch, tmp_path)
    ensure_harness()
    skill = create_skill("review", "Review code changes", "Check tests and edge cases.")
    assert skill.name == "review"
    assert list_skills()[0].description == "Review code changes"
    assert "edge cases" in read_skill("review").content

    updated = update_skill("review", content="Check tests, edge cases, and docs.")
    assert "docs" in updated.content
    try:
        create_skill("review", "duplicate", "different")
    except ValueError as exc:
        assert "exists" in str(exc)
    else:
        raise AssertionError("duplicate create should fail")

    try:
        create_skill("secret", "bad", "api_key=sk-" + "x" * 30)
    except ValueError as exc:
        assert "credential" in str(exc)
    else:
        raise AssertionError("secret should be rejected")

    assert delete_skill("review") is True
    assert list_skills() == []


def test_memory_and_quick_notes(tmp_path: Path, monkeypatch):
    use_home(monkeypatch, tmp_path)
    ensure_harness()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    path, changed = write_memory(workspace, target="user", content="Use pytest.")
    assert changed and path == user_memory_path()
    _, changed = write_memory(workspace, target="user", content="Use pytest.")
    assert changed is False
    write_memory(workspace, target="user", content="Keep answers short.", mode="replace")
    assert read_memory(workspace).user.content == "Keep answers short.\n"

    project_path, created, _ = init_project_memory(workspace)
    assert created and project_path.exists()
    _, created, _ = init_project_memory(workspace)
    assert created is False

    agents_path, agents_created, agents_content = init_agents_file(workspace)
    assert agents_created and agents_path.name == "AGENTS.md"
    assert "## Working agreements" in agents_content
    _, agents_created, _ = init_agents_file(workspace)
    assert agents_created is False

    note_path, changed = quick_note("remember the parser fix")
    assert changed and note_path.exists()
    assert "parser fix" in note_path.read_text(encoding="utf-8")

    try:
        write_memory(workspace, target="user", content="token=" + "a" * 30)
    except ValueError as exc:
        assert "credential" in str(exc)
    else:
        raise AssertionError("secret memory should be rejected")


def test_memory_skill_and_recall_tools(tmp_path: Path, monkeypatch):
    use_home(monkeypatch, tmp_path)
    ensure_harness()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = make_context(workspace)

    memory = asyncio.run(
        MemoryWriteTool().run(context, target="user", content="Always run the test suite.")
    )
    assert memory.ok
    assert "Always run the test suite." in user_memory_path().read_text(encoding="utf-8")

    created = asyncio.run(
        SkillManageTool().run(
            context,
            action="create",
            name="verify",
            description="Verify a change",
            content="Run tests and inspect the diff.",
        )
    )
    assert created.ok
    viewed = asyncio.run(SkillViewTool().run(context, name="verify"))
    assert viewed.ok and "inspect the diff" in viewed.output
    patched = asyncio.run(
        SkillManageTool().run(
            context,
            action="patch",
            name="verify",
            old_string="inspect the diff",
            new_string="inspect the diff and run tests",
        )
    )
    assert patched.ok
    assert "run tests" in read_skill("verify").content

    store = Store(tmp_path / "state.db")
    try:
        old = store.create_session(cwd=str(workspace), model="mock/model-a", title="old parser")
        store.append_message(
            old.id,
            Message(
                role="user",
                content="The database migration failed with a lock; token=sk-" + "z" * 30,
            ),
        )
        current = store.create_session(cwd=str(workspace), model="mock/model-a")
        store.append_message(current.id, Message(role="user", content="A different current task"))
        recall_context = make_context(workspace, store=store, session_id=current.id)
        recalled = asyncio.run(
            RecallTool().run(recall_context, query="database migration!", limit=5)
        )
        assert recalled.ok
        assert old.id in recalled.output
        assert current.id not in recalled.output
        assert "sk-" not in recalled.output
    finally:
        store.close()


@pytest.mark.asyncio
async def test_agent_loop_uses_the_external_system_prompt(config, store: Store, sample_repo: Path):
    from .conftest import ScriptedAdapter, make_loop, outcome

    root = ensure_harness()
    (root / "prompts" / "system.md").write_text(
        "HARNESS PROMPT for {{workspace}}\n", encoding="utf-8"
    )
    adapter = ScriptedAdapter([outcome(text="ok")], config.providers["mock"])
    loop = make_loop(config, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")

    async def emit(_event_type: str, _data: dict) -> None:
        return None

    result = await loop.run(session, "hello", emit=emit)
    assert result.error is None
    system = adapter.requests[0].messages[0]
    assert system.role == "system"
    assert (system.content or "").startswith("HARNESS PROMPT")
    assert str(sample_repo.resolve()) in (system.content or "")


@pytest.mark.asyncio
async def test_agent_created_skill_appears_in_the_next_session(
    config, store: Store, sample_repo: Path
):
    from .conftest import ScriptedAdapter, make_loop, outcome, tool_call

    first = ScriptedAdapter(
        [
            outcome(
                tool_calls=[
                    tool_call(
                        "skill_manage",
                        {
                            "action": "create",
                            "name": "learned",
                            "description": "A learned procedure",
                            "content": "Run the learned check.",
                        },
                    )
                ]
            ),
            outcome(text="Skill saved."),
        ],
        config.providers["mock"],
    )
    loop = make_loop(config, store, first)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")

    async def emit(_event_type: str, _data: dict) -> None:
        return None

    result = await loop.run(session, "Save a reusable procedure", emit=emit)
    assert result.error is None
    assert "learned" in [skill.name for skill in list_skills()]

    second = ScriptedAdapter([outcome(text="Second session.")], config.providers["mock"])
    next_loop = make_loop(config, store, second)
    next_session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    await next_loop.run(next_session, "hello again", emit=emit)
    system = second.requests[0].messages[0].content or ""
    assert "learned: A learned procedure" in system


def test_system_prompt_includes_configured_mcp_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from siwah.mcp.client import save_mcp_config
    from siwah.paths import harness_dir

    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    ensure_harness()
    save_mcp_config(
        {
            "servers": {
                "audit": {
                    "transport": "stdio",
                    "command": [sys.executable, "server.py"],
                    "description": "Audit tools",
                    "env": {"TOKEN": "SECRET"},
                }
            }
        },
        harness_dir(),
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    prompt = build_system_prompt(workspace)
    assert "MCP servers" in prompt
    assert "audit (stdio)" in prompt
    assert "Audit tools" in prompt
    assert "SECRET" not in prompt


def test_harness_upgrade_preserves_user_edits(tmp_path: Path):
    from siwah.harness.bootstrap import _upgrade_default
    from siwah.harness.defaults import (
        DEFAULT_SYSTEM_PROMPT,
        LEGACY_SYSTEM_PROMPTS,
    )

    path = tmp_path / "system.md"
    for prior_default in LEGACY_SYSTEM_PROMPTS:
        path.write_text(prior_default, encoding="utf-8")
        _upgrade_default(path, DEFAULT_SYSTEM_PROMPT, LEGACY_SYSTEM_PROMPTS)
        assert path.read_text(encoding="utf-8") == DEFAULT_SYSTEM_PROMPT

    path.write_text("my own prompt\n", encoding="utf-8")
    _upgrade_default(path, DEFAULT_SYSTEM_PROMPT, LEGACY_SYSTEM_PROMPTS)
    assert path.read_text(encoding="utf-8") == "my own prompt\n"


def test_default_prompt_teaches_clarification_before_edits(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    prompt = build_system_prompt(tmp_path)
    assert "use ask_user before\n  making changes" in prompt
    assert "dark-mode form" in prompt
    assert "ask whether it is for login, registration, contact" in prompt
    assert "answer arrives through the question panel" in prompt
    assert "Set background=true only when you can" in prompt
    assert "Omit background when you" in prompt


async def test_recall_window_uses_project_and_compaction_boundary(tmp_path, monkeypatch):
    from siwah.core.storage import Store
    from siwah.core.types import Message
    from siwah.tools.harness import RecallTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    store = Store(tmp_path / "state.db")
    try:
        session = store.create_session(cwd=str(workspace), model="mock/model-a")
        other = store.create_session(cwd=str(tmp_path), model="mock/model-a")
        old = store.append_message(session.id, Message(role="user", content="unique database rule"))
        store.set_context_summary(session.id, "compact", 1)
        store.append_message(session.id, Message(role="user", content="unique recent rule"))
        foreign = store.append_message(
            other.id, Message(role="user", content="unique private rule")
        )
        context = make_context(workspace, store=store, session_id=session.id)
        result = await RecallTool().run(context, query="unique")
        assert old in result.output
        assert "recent" not in result.output and "private" not in result.output
        assert "database rule" in (await RecallTool().run(context, message_id=old)).output
        assert "private" not in (await RecallTool().run(context, message_id=foreign)).output
    finally:
        store.close()
