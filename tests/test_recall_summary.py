from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from siwah.config import AgentConfig, ConfigError, PermissionsConfig
from siwah.core.storage import Store
from siwah.core.types import Message, StreamOutcome, Usage
from siwah.permissions.policy import PermissionPolicy
from siwah.tools.base import ToolContext
from siwah.tools.harness import RecallTool


class FakeRouter:
    def __init__(self, text: str = "Use the signed-cookie decision from March.") -> None:
        self.text = text
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def stream(self, role: str, **kwargs: Any) -> StreamOutcome:
        self.calls.append((role, kwargs))
        return StreamOutcome(
            provider="mock",
            model="fast-model",
            text=self.text,
            usage=Usage(
                input_tokens=12,
                output_tokens=8,
                allowance=0.0005,
                billing_source="go",
            ),
            finish_reason="stop",
        )


class ErrorRouter:
    async def stream(self, _role: str, **_kwargs: Any) -> StreamOutcome:
        raise ConfigError("No model configured for role 'fast'")


def _context(
    workspace: Path,
    store: Store,
    session_id: str,
    router: Any,
) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        policy=PermissionPolicy(workspace, PermissionsConfig(mode="ask")),
        session_id=session_id,
        agent_config=AgentConfig(),
        store=store,
        router=router,
    )


def _seed(store: Store, workspace: Path) -> tuple[str, str, str]:
    old = store.create_session(cwd=str(workspace), model="mock/model-a", title="auth decision")
    message_id = store.append_message(
        old.id,
        Message(
            role="user",
            content=(
                "We decided to use signed cookies for auth. "
                "The temporary token was token=sk-" + "q" * 30
            ),
        ),
    )
    current = store.create_session(cwd=str(workspace), model="mock/model-a", title="current")
    store.append_message(current.id, Message(role="user", content="Unrelated current task"))
    return old.id, message_id, current.id


def test_recall_summarize_uses_fast_model_and_preserves_ids(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "state.db")
    try:
        old_id, message_id, current_id = _seed(store, workspace)
        router = FakeRouter()
        context = _context(workspace, store, current_id, router)
        result = asyncio.run(
            RecallTool().run(context, query="signed cookies", summarize=True, limit=5)
        )
        assert result.ok
        assert "signed-cookie decision" in result.output
        assert old_id in result.output
        assert message_id in result.output
        assert "Sources:" in result.output
        assert result.data is not None and result.data["summarized"] is True

        role, kwargs = router.calls[0]
        assert role == "fast"
        evidence = kwargs["messages"][1].content
        assert "signed cookies" in evidence
        assert message_id in evidence
        assert "sk-" not in evidence

        usage = store.usage_totals(current_id)
        assert usage.input_tokens == 12
        assert usage.output_tokens == 8
    finally:
        store.close()


def test_recall_summarize_falls_back_when_fast_role_is_missing(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "state.db")
    try:
        old_id, message_id, current_id = _seed(store, workspace)
        context = _context(workspace, store, current_id, ErrorRouter())
        result = asyncio.run(
            RecallTool().run(context, query="signed cookies", summarize=True, limit=5)
        )
        assert result.ok
        assert "summary unavailable" in result.output
        assert old_id in result.output
        assert message_id in result.output
        assert result.data is not None and result.data["summarized"] is False
    finally:
        store.close()


def test_recall_summarize_does_not_call_the_model_without_matches(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "state.db")
    try:
        session = store.create_session(cwd=str(workspace), model="mock/model-a")
        router = FakeRouter()
        context = _context(workspace, store, session.id, router)
        result = asyncio.run(
            RecallTool().run(context, query="nothing-matches-this", summarize=True)
        )
        assert result.output == "(no matching sessions)"
        assert router.calls == []
    finally:
        store.close()


def test_recall_summarize_rejects_message_id_mode(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "state.db")
    try:
        session = store.create_session(cwd=str(workspace), model="mock/model-a")
        context = _context(workspace, store, session.id, FakeRouter())
        result = asyncio.run(RecallTool().run(context, message_id="msg_missing", summarize=True))
        assert result.ok is False
        assert "summarize applies to query searches" in result.output
    finally:
        store.close()
