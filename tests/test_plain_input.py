from __future__ import annotations

import asyncio
import threading

import pytest

from siwah.cli.plain_ui import PlainRenderer
from siwah.permissions.policy import PermissionRequest


@pytest.mark.asyncio
@pytest.mark.parametrize("permission", [False, True])
async def test_plain_prompt_cancellation_does_not_hold_default_executor(monkeypatch, permission):
    entered = threading.Event()
    release = threading.Event()

    def blocking_input(_prompt):
        entered.set()
        release.wait(timeout=5)
        return "o"

    def forbidden_executor(*_args, **_kwargs):
        raise AssertionError("Blocking terminal input must not hold asyncio's shutdown executor")

    monkeypatch.setattr("builtins.input", blocking_input)
    monkeypatch.setattr(asyncio.get_running_loop(), "run_in_executor", forbidden_executor)
    renderer = PlainRenderer()
    operation = (
        renderer.ask_permission(
            PermissionRequest(tool="bash", kind="bash", preview="$ test"), "test"
        )
        if permission
        else renderer.ask_user("A question")
    )
    task = asyncio.create_task(operation)
    try:
        # Yield until either the reader starts or an implementation error finishes the task.
        for _ in range(100):
            if entered.is_set() or task.done():
                break
            await asyncio.sleep(0.005)
        if task.done():
            await task
        assert entered.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_plain_prompts_accept_answers_and_handle_eof(monkeypatch):
    renderer = PlainRenderer()
    monkeypatch.setattr("builtins.input", lambda _prompt: "s")
    request = PermissionRequest(tool="bash", kind="bash", preview="$ test")
    approval = await renderer.ask_permission(request, "test")
    assert approval.decision == "session"
    assert await renderer.ask_user("Question") == "s"

    def eof(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert (await renderer.ask_permission(request, "test")).decision == "deny"
    assert await renderer.ask_user("Question") == ""
