from __future__ import annotations

import asyncio
import json
from pathlib import Path

from siwah.config import Config
from siwah.core.storage import Store
from siwah.core.types import Message
from siwah.core.verify import VerifyReport
from siwah.harness.bootstrap import ensure_harness
from siwah.permissions.policy import PermissionPolicy
from siwah.tools.base import ToolContext
from siwah.tools.status import SelfStatusTool
from siwah.tools.verify import VerifyTool

from .conftest import ScriptedAdapter, make_loop


def test_self_status_reports_runtime_context(
    config: Config, store: Store, sample_repo: Path, monkeypatch
):
    monkeypatch.setenv("SIWAH_HOME", str(sample_repo.parent / "status-home"))
    ensure_harness()
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a", title="status")
    context = ToolContext(
        workspace=sample_repo,
        policy=PermissionPolicy(sample_repo, config.permissions),
        session_id=session.id,
        agent_config=config.agent,
        store=store,
        context_tokens=12_000,
        context_percent=42.5,
        context_window=28_000,
        allowance=0.125,
        tool_names=["read_file", "self_status"],
        step=3,
    )

    result = asyncio.run(SelfStatusTool().run(context))

    assert result.ok
    payload = json.loads(result.output)
    assert payload["version"]
    assert payload["session"]["id"] == session.id
    assert payload["context"] == {"tokens": 12_000, "percent": 42.5, "window": 28_000}
    assert payload["allowance"] == 0.125
    assert payload["tools"] == ["read_file", "self_status"]
    assert payload["permission_mode"] == config.permissions.mode
    assert payload["memory"]["user_chars"] == 0
    assert payload["jobs"] == []
    assert payload["background_subagents"] == 0


def test_verify_tool_runs_the_session_verifier(
    config: Config, store: Store, sample_repo: Path, monkeypatch
):
    class FakeVerifier:
        async def run(self) -> VerifyReport:
            return VerifyReport(status="passed", summary="checks passed")

    monkeypatch.setenv("SIWAH_HOME", str(sample_repo.parent / "verify-home"))
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    context = ToolContext(
        workspace=sample_repo,
        policy=PermissionPolicy(sample_repo, config.permissions),
        session_id=session.id,
        agent_config=config.agent,
        store=store,
        verifier=FakeVerifier(),  # type: ignore[arg-type]
    )
    result = asyncio.run(VerifyTool().run(context))
    assert result.ok
    assert result.data == {"status": "passed", "commands": []}
    assert context.verify_report is not None
    assert context.verify_report.summary == "checks passed"


def test_context_notice_is_append_only_and_once_per_threshold(config: Config, store: Store):
    loop = make_loop(config, store, ScriptedAdapter([], config.providers["mock"]))
    messages: list[Message] = []
    seen: set[int] = set()
    loop._append_context_notice(messages, 69.0, seen)
    assert messages == []
    loop._append_context_notice(messages, 72.0, seen)
    loop._append_context_notice(messages, 76.0, seen)
    assert len(messages) == 1
    loop._append_context_notice(messages, 86.0, seen)
    assert len(messages) == 2


def test_self_status_uses_no_arguments():
    assert SelfStatusTool().parameters == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
