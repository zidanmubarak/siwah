from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from siwah.config import AgentConfig, ContextConfig, PermissionsConfig
from siwah.core.context import (
    STUB_PREFIX,
    compaction_prompt,
    fallback_summary,
    prune_tool_messages,
)
from siwah.core.storage import Store
from siwah.core.types import Message, StreamOutcome, Usage
from siwah.permissions.policy import Approval, PermissionRequest
from siwah.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from siwah.tools.output import ToolOutputTool

from .conftest import ScriptedAdapter, make_loop, outcome, tool_call


def test_compaction_redacts_all_transcript_sources_before_truncating():
    secret = "sk-" + "testcredential" * 4
    messages = [
        Message(
            role="assistant",
            content="Inspect the configuration",
            tool_calls=[tool_call("bash", {"command": "x" * 279 + " " + secret})],
        )
    ]
    prompt = compaction_prompt(
        messages, f"[pending] inspect {secret}", previous_summary=f"Earlier token: {secret}"
    )
    serialized = "\n".join(message.content or "" for message in prompt)
    assert secret not in serialized
    assert "sk-testcredential" not in serialized
    assert "[REDACTED]" in serialized
    assert secret in messages[0].tool_calls[0].arguments["command"]


def _context(workspace: Path, **kwargs) -> ToolContext:
    from siwah.permissions.policy import PermissionPolicy

    async def approver(_request: PermissionRequest) -> Approval:
        return Approval(decision="once")

    return ToolContext(
        workspace=workspace,
        policy=PermissionPolicy(workspace, PermissionsConfig(mode="auto-edit")),
        session_id="ses_context",
        agent_config=AgentConfig(tool_output_limit=200),
        approver=approver,
        **kwargs,
    )


class LongOutputTool(Tool):
    name = "long_output"
    description = "Return a deliberately long result."
    kind = "read"
    parameters = {"type": "object", "properties": {}}

    async def run(self, context: ToolContext, **arguments):
        return ToolResult.success("x" * 5000)


def test_large_output_is_offloaded_and_reopened(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = Store(tmp_path / "state.db")
    context = _context(workspace, store=store, tool_call_id="call_long")
    result = asyncio.run(ToolRegistry([LongOutputTool()]).execute("long_output", context, {}))
    assert result.ok
    assert "handle=call_long" in result.output
    assert result.data and result.data.get("full_output") == "x" * 5000
    output_path = Path(str(result.data.get("output_path")))
    assert output_path.exists()
    reopened = asyncio.run(ToolOutputTool().run(context, handle="call_long"))
    assert reopened.ok
    assert len(reopened.output) == 5000
    store.close()


def test_prune_tool_messages_keeps_recent_results():
    messages = [
        Message(role="tool", content="old-1", tool_call_id="call_1"),
        Message(role="tool", content="old-2", tool_call_id="call_2"),
        Message(role="tool", content="new-1", tool_call_id="call_3"),
        Message(role="tool", content="new-2", tool_call_id="call_4"),
    ]
    pruned = prune_tool_messages(messages, keep_recent=2)
    assert pruned == 2
    assert messages[0].content is not None and messages[0].content.startswith(STUB_PREFIX)
    assert "call_1" in (messages[0].content or "")
    assert messages[1].content is not None and messages[1].content.startswith(STUB_PREFIX)
    assert messages[2].content == "new-1"
    assert messages[3].content == "new-2"


def test_fallback_summary_is_structured():
    messages = [
        Message(role="user", content="Fix the parser"),
        Message(role="assistant", content="I will edit parser.py"),
        Message(role="tool", content="error: parser failed", tool_call_id="call_1"),
    ]
    summary = fallback_summary(messages, "[pending] 1: run tests")
    assert "## Goal" in summary
    assert "Fix the parser" in summary
    assert "## Decisions" in summary
    assert "## Open errors" in summary
    assert "parser failed" in summary


@pytest.mark.asyncio
async def test_stable_prefix_and_cache_read_ratio(config, store: Store, sample_repo: Path):
    cfg = config.model_copy(
        update={
            "agent": config.agent.model_copy(update={"max_steps": 6}),
        }
    )
    usage = Usage(input_tokens=1000, output_tokens=50, cache_read_tokens=800)
    adapter = ScriptedAdapter(
        [
            StreamOutcome(
                provider="mock",
                model="model-a",
                tool_calls=[tool_call("read_file", {"path": "math_utils.py"})],
                usage=usage,
                finish_reason="tool_calls",
            ),
            StreamOutcome(
                provider="mock",
                model="model-a",
                tool_calls=[
                    tool_call(
                        "edit_file",
                        {
                            "path": "math_utils.py",
                            "old_string": "return a - b",
                            "new_string": "return a + b",
                        },
                        "call_2",
                    )
                ],
                usage=usage,
                finish_reason="tool_calls",
            ),
            StreamOutcome(
                provider="mock",
                model="model-a",
                text="Done.",
                usage=usage,
                finish_reason="stop",
            ),
        ],
        cfg.providers["mock"],
    )
    loop = make_loop(cfg, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")

    async def approve(_request, _request_id):
        return Approval(decision="once")

    async def emit(_event_type: str, _data: dict) -> None:
        return None

    result = await loop.run(session, "Fix the test", emit=emit, permission_resolver=approve)
    assert result.error is None
    assert len(adapter.requests) == 3
    first = adapter.requests[0]
    for request in adapter.requests[1:]:
        assert request.messages[0].content == first.messages[0].content
        assert [tool.name for tool in request.tools] == [tool.name for tool in first.tools]
    ratio = result.usage.cache_read_tokens / max(1, result.usage.input_tokens)
    assert ratio >= 0.7


@pytest.mark.asyncio
async def test_auto_compaction_persists_a_summary(config, store: Store, sample_repo: Path):
    cfg = config.model_copy(
        update={
            "context": ContextConfig(
                keep_recent_tool_results=2,
                auto_compact_percent=10,
                max_context_tokens=1000,
            ),
            "agent": config.agent.model_copy(update={"max_steps": 4}),
        }
    )
    adapter = ScriptedAdapter([outcome(text="Done.")], cfg.providers["mock"])
    loop = make_loop(cfg, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")

    async def emit(_event_type: str, _data: dict) -> None:
        return None

    result = await loop.run(session, "Compact please", emit=emit)
    assert result.error is None
    updated = store.get_session(session.id)
    assert updated is not None
    assert updated.metadata.get("context_summary")
    context_messages = store.get_context_messages(session.id)
    assert context_messages[0].role == "system"
    assert "[compacted context]" in (context_messages[0].content or "")


@pytest.mark.asyncio
async def test_loop_prunes_old_tool_results(config, store: Store, sample_repo: Path):
    cfg = config.model_copy(
        update={
            "context": ContextConfig(keep_recent_tool_results=2),
            "agent": config.agent.model_copy(update={"max_steps": 12}),
        }
    )
    adapter = ScriptedAdapter(
        [
            *[
                outcome(tool_calls=[tool_call("read_file", {"path": "README.md"}, f"read_{i}")])
                for i in range(8)
            ],
            outcome(text="Done."),
        ],
        cfg.providers["mock"],
    )
    loop = make_loop(cfg, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")

    async def emit(_event_type: str, _data: dict) -> None:
        return None

    result = await loop.run(session, "Read a lot", emit=emit)
    assert result.error is None
    tool_messages = [message for message in adapter.requests[-1].messages if message.role == "tool"]
    assert len(tool_messages) == 8
    assert all((message.content or "").startswith(STUB_PREFIX) for message in tool_messages[:6])
    assert all(
        not (message.content or "").startswith(STUB_PREFIX) for message in tool_messages[-2:]
    )


@pytest.mark.asyncio
async def test_fast_role_compaction_uses_the_summary(config, store: Store, sample_repo: Path):
    from siwah.config import RolesConfig

    class CompactingAdapter(ScriptedAdapter):
        async def stream_chat(self, request, *, session_id, emit):
            system = request.messages[0].content or "" if request.messages else ""
            if "compact a coding-agent transcript" in system:
                from siwah.core.types import StreamOutcome

                self.requests.append(request)
                return StreamOutcome(
                    provider="mock",
                    model="model-a",
                    text="## Goal\nCompacted goal\n\n## Decisions\n- keep it short",
                    usage=Usage(input_tokens=10, output_tokens=5),
                    finish_reason="stop",
                )
            return await super().stream_chat(request, session_id=session_id, emit=emit)

    cfg = config.model_copy(
        update={
            "roles": RolesConfig(main="mock/model-a", fast="mock/model-a"),
            "context": ContextConfig(
                keep_recent_tool_results=2,
                auto_compact_percent=10,
                max_context_tokens=1000,
            ),
            "agent": config.agent.model_copy(update={"max_steps": 4}),
        }
    )
    adapter = CompactingAdapter([outcome(text="Done.")], cfg.providers["mock"])
    loop = make_loop(cfg, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")

    async def emit(_event_type: str, _data: dict) -> None:
        return None

    result = await loop.run(session, "Compact with the fast role", emit=emit)
    assert result.error is None
    updated = store.get_session(session.id)
    assert updated is not None
    assert "Compacted goal" in str(updated.metadata.get("context_summary"))
