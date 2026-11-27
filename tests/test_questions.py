from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from siwah.auth import AuthStore
from siwah.cli.plain_ui import PlainRenderer
from siwah.core.questions import Question, QuestionOption
from siwah.permissions.policy import PermissionPolicy
from siwah.providers.registry import ProviderRegistry
from siwah.server.app import create_app
from siwah.server.runtime import DaemonRuntime
from siwah.tools.base import ToolContext
from siwah.tools.runtime import AskUserTool

from .conftest import ScriptedAdapter, make_loop, outcome, tool_call


def _runtime(config, store, outcomes):
    adapter = ScriptedAdapter(outcomes, config.providers["mock"])
    auth = AuthStore()
    registry = ProviderRegistry(config, auth)
    registry._adapters["mock"] = adapter
    return DaemonRuntime(config, store=store, auth=auth, registry=registry), adapter


async def _event(runtime, session_id, name):
    async with asyncio.timeout(3):
        stream = runtime.bus(session_id).subscribe()
        try:
            async for event in stream:
                if event.type == name:
                    return event
        finally:
            await stream.aclose()
    raise AssertionError(f"Missing {name}")


def test_question_supports_multiple_answers_and_headers():
    question = Question(
        header="Scope",
        question="Which parts should change?",
        options=[QuestionOption(label="Unit"), QuestionOption(label="All")],
        multiple=True,
        allow_custom=False,
    )
    assert question.validate_answer("Unit\nAll\nUnit") == "Unit\nAll"
    with pytest.raises(ValueError):
        question.validate_answer("Missing")
    with pytest.raises(ValueError):
        Question(header="bad\nheader", question="Question?")


@pytest.mark.asyncio
async def test_ask_user_tool_asks_a_question_sequence(tmp_path, config):
    asked: list[str] = []

    async def answer(question: Question, _tool_call_id: str | None) -> str:
        asked.append(question.question)
        return "First" if len(asked) == 1 else "Second"

    context = ToolContext(
        workspace=tmp_path,
        policy=PermissionPolicy(tmp_path, config.permissions),
        session_id="session",
        agent_config=config.agent,
        ask_user=answer,
        tool_call_id="call",
    )
    result = await AskUserTool().run(
        context,
        questions=[
            {
                "header": "One",
                "question": "Pick the first.",
                "allow_custom": False,
                "options": [{"label": "First"}],
            },
            {
                "header": "Two",
                "question": "Pick the second.",
                "allow_custom": False,
                "options": [{"label": "Second"}],
            },
        ],
    )
    assert result.ok
    assert asked == ["Pick the first.", "Pick the second."]
    assert "One: First" in result.output and "Two: Second" in result.output


def test_questions_reject_bidi_and_format_controls():
    with pytest.raises(ValidationError):
        Question(question="choose \u202e safe")
    with pytest.raises(ValidationError):
        QuestionOption(label="safe\u2066option")


def test_ask_user_tool_schema_is_inline_for_provider_compatibility():
    schema = AskUserTool().parameters
    rendered = json.dumps(schema)
    assert "$defs" not in rendered
    assert "$ref" not in rendered
    assert schema["properties"]["options"]["items"]["properties"]["label"]["type"] == "string"


@pytest.mark.asyncio
async def test_tui_question_waits_for_user_choice_before_editing(config, store, sample_repo):
    prompt = (
        "Tolong buatkan satu file HTML CSS dengan halaman form bertema dark mode. "
        "Jika ada pertanyaan, boleh bertanya sebelum mengerjakan instruksi saya!"
    )
    component = sample_repo / "index.html"
    dark = (
        "<!doctype html><title>Login</title><style>body{color-scheme:dark}</style>"
        '<form><label>Email <input type="email"></label></form>\n'
    )
    runtime, adapter = _runtime(
        config,
        store,
        [
            outcome(
                tool_calls=[
                    tool_call(
                        "ask_user",
                        {
                            "question": "Form dark mode ini untuk keperluan apa?",
                            "options": [
                                {"label": "Form login", "description": "Masuk dengan email."},
                                {"label": "Form kontak"},
                            ],
                            "allow_custom": False,
                        },
                    )
                ]
            ),
            outcome(tool_calls=[tool_call("write_file", {"path": "index.html", "content": dark})]),
            outcome(text="Form login sudah memakai dark mode."),
        ],
    )
    session = runtime.create_session(cwd=str(sample_repo), model="mock/model-a")
    other = runtime.create_session(cwd=str(sample_repo), model="mock/model-a")
    session_path = f"/v1/sessions/{session.id}"
    transport = httpx.ASGITransport(app=create_app(config, runtime=runtime, token="test-token"))
    headers = {"Authorization": "Bearer test-token"}
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", headers=headers
        ) as client:
            response = await client.post(f"{session_path}/messages", json={"content": prompt})
            assert response.status_code == 200
            asked = await _event(runtime, session.id, "question.request")
            assert asked.data["question"] == "Form dark mode ini untuk keperluan apa?"
            assert asked.data["tool_call_id"]
            assert len(adapter.requests) == 1 and not component.exists()

            pending = await client.get(f"{session_path}/question")
            assert pending.json() == asked.data
            request_id = asked.data["request_id"]
            wrong = await client.post(
                f"/v1/sessions/{other.id}/questions/{request_id}", json={"answer": "Form login"}
            )
            assert wrong.status_code == 404
            reply_path = f"{session_path}/questions/{request_id}"
            invalid = await client.post(reply_path, json={"answer": "Missing option"})
            assert invalid.status_code == 400
            assert (await client.get(f"{session_path}/question")).json() == asked.data
            assert (await client.post(reply_path, json={"answer": "Form login"})).json() == {
                "ok": True
            }

            resolved = await _event(runtime, session.id, "question.resolved")
            assert resolved.data == {"request_id": request_id, "reason": "answered"}
            await _event(runtime, session.id, "run.end")
            assert (await client.get(f"{session_path}/question")).json() is None
            assert (await client.post(reply_path, json={"answer": "Form login"})).status_code == 404
    finally:
        await runtime.close()
    assert component.read_text(encoding="utf-8") == dark
    assert any(
        msg.role == "tool" and msg.content == "Form login" for msg in adapter.requests[1].messages
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("resolution", ["dismissed", "cancelled", "timeout", "answered"])
async def test_question_waiter_lifecycle(config, store, sample_repo, monkeypatch, resolution):
    runtime, adapter = _runtime(
        config,
        store,
        [
            outcome(tool_calls=[tool_call("ask_user", {"question": "What scope should I use?"})]),
            outcome(text="Acknowledged."),
        ],
    )
    session = runtime.create_session(cwd=str(sample_repo), model="mock/model-a")
    if resolution == "timeout":
        monkeypatch.setattr("siwah.server.runtime.QUESTION_TIMEOUT_S", 0.05)
    try:
        runtime.start_run(session, "Decide a scope.")
        asked = await _event(runtime, session.id, "question.request")
        identity = asked.data["request_id"]
        assert asked.data["options"] == [] and asked.data["allow_custom"] is True
        if resolution == "dismissed":
            assert runtime.answer_question(session.id, identity, None)
        elif resolution == "cancelled":
            assert await runtime.abort(session.id)
            assert runtime.answer_question(session.id, identity, "too late") is False
        elif resolution == "answered":
            assert runtime.answer_question(session.id, identity, " Only the form. ")
        event = await _event(runtime, session.id, "question.resolved")
        assert event.data == {"request_id": identity, "reason": resolution}
        await _event(runtime, session.id, "run.end")
        assert runtime.pending_question(session.id) is None
        if resolution == "answered":
            assert any(
                msg.role == "tool" and msg.content == "Only the form."
                for msg in adapter.requests[1].messages
            )
        elif resolution != "cancelled":
            assert any(
                msg.role == "tool" and "without an answer" in (msg.content or "")
                for msg in adapter.requests[1].messages
            )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_noninteractive_run_does_not_advertise_questions(config, store, sample_repo):
    adapter = ScriptedAdapter(
        [
            outcome(tool_calls=[tool_call("ask_user", {"question": "Question?"})]),
            outcome(text="No interactive input."),
        ],
        config.providers["mock"],
    )
    agent = make_loop(config, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")

    async def discard(_kind, _data):
        pass

    await agent.run(session, "Complete a noninteractive job.", emit=discard)
    assert all(tool.name != "ask_user" for tool in adapter.requests[0].tools)
    assert any(
        msg.role == "tool" and "non-interactive" in (msg.content or "")
        for msg in adapter.requests[1].messages
    )


@pytest.mark.asyncio
async def test_plain_renderer_accepts_choice_and_rejects_unknown_label(monkeypatch, capsys):
    values = iter(["unknown", "2"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(values))
    question = Question(
        question="Choose a scope.",
        options=[
            QuestionOption(label="All pages"),
            QuestionOption(label="Login only", description="Leave other pages alone."),
        ],
        allow_custom=False,
    )
    assert await PlainRenderer().ask_user(question) == "Login only"
    rendered = capsys.readouterr().out
    assert "1. All pages" in rendered
    assert "Login only" in rendered and "Leave other pages alone." in rendered
    assert "Select one of the listed answers" in rendered


@pytest.mark.asyncio
async def test_plain_renderer_accepts_multiple_choices(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _prompt: "1,2")
    question = Question(
        header="Scope",
        question="Choose the scopes.",
        options=[QuestionOption(label="Unit"), QuestionOption(label="All")],
        multiple=True,
        allow_custom=False,
    )
    assert await PlainRenderer().ask_user(question) == "Unit\nAll"
    assert "[Scope]" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_question_redacts_credential_like_answers_before_tool_events(
    config, store, sample_repo
):
    runtime, adapter = _runtime(
        config,
        store,
        [
            outcome(tool_calls=[tool_call("ask_user", {"question": "Which environment?"})]),
            outcome(text="Acknowledged."),
        ],
    )
    session = runtime.create_session(cwd=str(sample_repo), model="mock/model-a")
    fixture_secret = "ghp_" + "test" * 8
    try:
        runtime.start_run(session, "Choose an environment.")
        asked = await _event(runtime, session.id, "question.request")
        assert runtime.answer_question(
            session.id, asked.data["request_id"], f"Staging: {fixture_secret}"
        )
        await _event(runtime, session.id, "run.end")
        assert fixture_secret not in str(runtime.bus(session.id).history())
        assert fixture_secret not in str(runtime.messages(session.id))
        assert any(
            msg.role == "tool" and msg.content == "Staging: [REDACTED]"
            for msg in adapter.requests[1].messages
        )
    finally:
        await runtime.close()
