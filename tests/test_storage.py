from __future__ import annotations

from siwah.core.storage import Store
from siwah.core.types import Message, MessageAttachment, ToolCall, Usage
from siwah.paths import trajectories_dir


def test_image_attachments_roundtrip_and_openai_parts(store: Store):
    session = store.create_session(cwd=".", model="mock/model-a")
    attachment = MessageAttachment(mime="image/png", data="YWJj", name="pasted.png")
    store.append_message(
        session.id, Message(role="user", content="describe this", attachments=[attachment])
    )
    stored = store.get_messages(session.id)[0]
    assert stored.attachments == [attachment]
    view = store.get_message_views(session.id)[0]
    assert view["attachments"][0]["mime"] == "image/png"
    payload = stored.to_openai()
    assert payload["content"] == [
        {"type": "text", "text": "describe this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,YWJj"}},
    ]


def test_sessions_messages_fts_and_usage(store: Store):
    session = store.create_session(cwd=".", model="mock/model-a", title="Demo")
    assert store.get_session(session.id) is not None
    store.append_message(session.id, Message(role="user", content="fix the parser bug"))
    store.append_message(
        session.id,
        Message(
            role="assistant",
            content="working",
            tool_calls=[ToolCall(id="call_1", name="read_file", arguments={"path": "a.py"})],
        ),
    )
    store.append_message(
        session.id, Message(role="tool", content="file contents", tool_call_id="call_1")
    )
    messages = store.get_messages(session.id)
    assert [message.role for message in messages] == ["user", "assistant", "tool"]
    assert messages[1].tool_calls[0].name == "read_file"
    hits = store.search_messages("parser")
    assert hits and hits[0]["session_id"] == session.id

    store.record_tool_call(
        session_id=session.id,
        tool_call_id="call_1",
        message_id="msg_x",
        name="read_file",
        arguments={"path": "a.py"},
        result={"ok": True, "output": "x"},
        status="ok",
    )
    assert len(store.get_tool_calls(session.id)) == 1

    store.record_usage(
        session_id=session.id,
        step_id="step_1",
        provider="mock",
        model="model-a",
        usage=Usage(input_tokens=10, output_tokens=2, allowance=0.001, cache_read_tokens=5),
    )
    store.record_usage(
        session_id=session.id,
        step_id="step_2",
        provider="mock",
        model="model-a",
        usage=Usage(input_tokens=4, output_tokens=1, allowance=0.002),
    )
    totals = store.usage_totals(session.id)
    assert totals.input_tokens == 14
    assert totals.allowance == 0.003
    assert totals.cache_read_tokens == 5
    assert totals.steps == 2

    store.append_trajectory(session.id, {"type": "note", "value": "x"})
    trajectory = trajectories_dir() / f"{session.id}.jsonl"
    assert trajectory.exists()


def test_message_views_restore_edit_diffs(store: Store):
    session = store.create_session(cwd=".", model="mock/model-a", title="Demo")
    message_id = store.append_message(
        session.id,
        Message(
            role="assistant",
            content="edited",
            tool_calls=[ToolCall(id="call_edit", name="edit_file", arguments={"path": "hello.py"})],
        ),
    )
    store.record_tool_call(
        session_id=session.id,
        tool_call_id="call_edit",
        message_id=message_id,
        name="edit_file",
        arguments={"path": "hello.py"},
        result={
            "ok": True,
            "data": {
                "diff": "--- a/hello.py\n+++ b/hello.py\n",
                "full_output": "full file content",
            },
        },
        status="ok",
    )

    view = next(item for item in store.get_message_views(session.id) if item["id"] == message_id)
    assert view["diffs"] == [{"path": "hello.py", "patch": "--- a/hello.py\n+++ b/hello.py\n"}]
    assert view["tool_outputs"] == {"call_edit": "full file content"}


def test_write_file_view_strips_legacy_verifier_suffix(store: Store):
    session = store.create_session(cwd=".", model="mock/model-a", title="Demo")
    message_id = store.append_message(
        session.id,
        Message(
            role="assistant",
            tool_calls=[
                ToolCall(id="call_write", name="write_file", arguments={"path": "hello.py"})
            ],
        ),
    )
    store.record_tool_call(
        session_id=session.id,
        tool_call_id="call_write",
        message_id=message_id,
        name="write_file",
        arguments={"path": "hello.py"},
        result={
            "ok": True,
            "data": {"full_output": "file body\n\nVerifier: unavailable"},
        },
        status="ok",
    )

    view = next(item for item in store.get_message_views(session.id) if item["id"] == message_id)
    assert view["tool_outputs"] == {"call_write": "file body"}
