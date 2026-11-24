from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from siwah.config import PermissionsConfig
from siwah.core.storage import Store
from siwah.harness.memory import read_memory
from siwah.permissions.policy import Approval, PermissionRequest

from .conftest import ScriptedAdapter, make_loop, outcome, tool_call


def approve_all():
    async def resolver(request: PermissionRequest, request_id: str) -> Approval:
        return Approval(decision="once")

    return resolver


def session_todos(store: Store, session_id: str) -> list[dict]:
    stored = store.get_session(session_id)
    assert stored is not None
    return stored.metadata["todos"]


async def test_batched_reads_overlap_and_keep_message_order(
    config, store, sample_repo, monkeypatch
):
    paths = {sample_repo / "README.md", sample_repo / "math_utils.py"}
    barrier = threading.Barrier(2, timeout=3)
    original = Path.read_bytes

    def read_bytes(path):
        if path in paths:
            barrier.wait()
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    adapter = ScriptedAdapter(
        [
            outcome(
                tool_calls=[
                    tool_call("read_file", {"path": "README.md"}, "first"),
                    tool_call("read_file", {"path": "math_utils.py"}, "second"),
                ]
            ),
            outcome(text="Done."),
        ],
        config.providers["mock"],
    )
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")

    async def emit(_event, _data):
        pass

    result = await make_loop(config, store, adapter).run(session, "Read both files", emit=emit)
    assert result.error is None
    calls = store.get_tool_calls(session.id)
    assert [(call["arguments"]["path"], call["status"]) for call in calls] == [
        ("README.md", "ok"),
        ("math_utils.py", "ok"),
    ]
    assert [
        message.tool_call_id for message in adapter.requests[-1].messages if message.role == "tool"
    ] == [call["id"] for call in calls]


@pytest.mark.asyncio
async def test_e2e_mock_fixes_bug_and_runs_tests(config, store: Store, sample_repo: Path):
    adapter = ScriptedAdapter(
        [
            outcome(
                tool_calls=[tool_call("read_file", {"path": "math_utils.py"})], allowance=0.001
            ),
            outcome(
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
                allowance=0.001,
            ),
            outcome(text="Fixed the failing test.", allowance=0.001),
        ],
        config.providers["mock"],
    )
    loop = make_loop(config, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    events: list[tuple[str, dict]] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    result = await loop.run(
        session,
        "Fix the failing test in math_utils.py",
        emit=emit,
        permission_resolver=approve_all(),
    )
    assert result.error is None
    assert result.text == "Fixed the failing test."
    assert result.steps == 3
    assert result.usage.allowance == pytest.approx(0.003)
    assert result.usage.steps == 3

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=sample_repo,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

    messages = store.get_messages(session.id)
    assert [message.role for message in messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]
    assert len(store.get_tool_calls(session.id)) == 2
    event_types = [event_type for event_type, _data in events]
    assert "message.delta" in event_types
    assert event_types.count("tool.start") == 2
    assert event_types.count("tool.end") == 2
    assert event_types.count("usage") == 3
    assert event_types[-1] == "run.end"


@pytest.mark.asyncio
async def test_permission_denial_is_fed_back(config, store: Store, sample_repo: Path):
    config = config.model_copy(update={"permissions": PermissionsConfig(mode="ask")})
    adapter = ScriptedAdapter(
        [
            outcome(
                tool_calls=[
                    tool_call(
                        "edit_file",
                        {
                            "path": "math_utils.py",
                            "old_string": "return a - b",
                            "new_string": "return a + b",
                        },
                    )
                ]
            ),
            outcome(text="I could not edit the file."),
        ],
        config.providers["mock"],
    )
    loop = make_loop(config, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    requests: list[PermissionRequest] = []

    async def deny(request: PermissionRequest, request_id: str) -> Approval:
        requests.append(request)
        return Approval(decision="deny", reason="not this time")

    async def emit(_event_type: str, _data: dict) -> None:
        return None

    result = await loop.run(
        session,
        "Fix the test",
        emit=emit,
        permission_resolver=deny,
    )
    assert result.error is None
    assert len(requests) == 1
    assert requests[0].tool == "edit_file"
    tool_messages = [m for m in store.get_messages(session.id) if m.role == "tool"]
    assert "not this time" in (tool_messages[0].content or "")
    assert (sample_repo / "math_utils.py").read_text() == "def add(a, b):\n    return a - b\n"


@pytest.mark.asyncio
async def test_cancellation_emits_run_end(config, store: Store, sample_repo: Path):
    class SlowAdapter(ScriptedAdapter):
        async def stream_chat(self, request, *, session_id, emit):
            await asyncio.sleep(10)
            return outcome(text="late")

    adapter = SlowAdapter([], config.providers["mock"])
    loop = make_loop(config, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    events: list[tuple[str, dict]] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    task = asyncio.create_task(
        loop.run(
            session,
            "slow task",
            emit=emit,
            permission_resolver=approve_all(),
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ("run.end", {"reason": "aborted"}) in events
    after = store.get_session(session.id)
    assert after is not None
    assert after.status == "idle"


@pytest.mark.asyncio
async def test_max_steps(config, store: Store, sample_repo: Path):
    config = config.model_copy(update={"agent": config.agent.model_copy(update={"max_steps": 1})})
    adapter = ScriptedAdapter(
        [outcome(tool_calls=[tool_call("read_file", {"path": "README.md"})])],
        config.providers["mock"],
    )
    loop = make_loop(config, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")

    async def emit(_event_type: str, _data: dict) -> None:
        return None

    result = await loop.run(session, "loop", emit=emit, permission_resolver=approve_all())
    assert result.error == "max_steps"
    assert result.steps == 1


@pytest.mark.asyncio
async def test_interactive_runs_tolerate_an_old_agent_config_snapshot(
    config, store: Store, sample_repo: Path
):
    # A preview daemon may load a newer loop.py with an older config module.
    # Missing interactive_max_steps must fall back to "no cap", not crash.
    data = config.agent.model_dump()
    data.pop("interactive_max_steps")
    agent = SimpleNamespace(**data)
    old_config = config.model_copy(update={"agent": cast(Any, agent)})
    adapter = ScriptedAdapter([outcome(text="Done.")], config.providers["mock"])
    loop = make_loop(old_config, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")

    async def emit(_event_type: str, _data: dict) -> None:
        return None

    result = await loop.run(
        session,
        "compatibility check",
        emit=emit,
        permission_resolver=approve_all(),
        extend_budget=True,
    )
    assert result.error is None
    assert result.text == "Done."


@pytest.mark.asyncio
async def test_interactive_runs_are_not_capped_by_the_hard_step_budget(
    config, store: Store, sample_repo: Path
):
    config = config.model_copy(
        update={
            "agent": config.agent.model_copy(
                update={
                    "max_steps": 2,
                    "interactive_max_steps": 0,
                    "self_check_min_steps": 999,
                    "max_completion_nudges": 0,
                }
            )
        }
    )
    calls = [
        outcome(tool_calls=[tool_call("read_file", {"path": f"file_{index}.txt"}, f"call_{index}")])
        for index in range(55)
    ]
    for index in range(55):
        (sample_repo / f"file_{index}.txt").write_text(f"File {index}", encoding="utf-8")
    adapter = ScriptedAdapter(
        [*calls, outcome(text="Finished after a long task.")], config.providers["mock"]
    )
    loop = make_loop(config, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")

    async def emit(_event_type: str, _data: dict) -> None:
        return None

    result = await loop.run(
        session,
        "long task",
        emit=emit,
        permission_resolver=approve_all(),
        extend_budget=True,
    )
    assert result.error is None
    assert result.steps == 56
    assert result.text == "Finished after a long task."


def test_edit_diff_hunk_counts_match_lines():
    from siwah.core.loop import edit_diff

    patch = edit_diff("math_utils.py", "return a - b", "return a + b")
    assert "@@ -1,1 +1,1 @@" in patch
    assert "-return a - b" in patch
    assert "+return a + b" in patch

    multiline = edit_diff("x.py", "a\nb", "a\nc\nd")
    assert "@@ -1,2 +1,3 @@" in multiline
    assert "-a\n-b" in multiline
    assert "+a\n+c\n+d" in multiline


@pytest.mark.asyncio
async def test_write_file_new_file_does_not_emit_an_empty_diff(
    config, store: Store, sample_repo: Path
):
    adapter = ScriptedAdapter(
        [
            outcome(
                tool_calls=[
                    tool_call(
                        "write_file",
                        {"path": "new_module.py", "content": "value = 1\n"},
                    )
                ]
            ),
            outcome(text="Created the module."),
        ],
        config.providers["mock"],
    )
    loop = make_loop(config, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    events: list[tuple[str, dict]] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    result = await loop.run(
        session, "create a module", emit=emit, permission_resolver=approve_all()
    )
    assert result.error is None
    assert (sample_repo / "new_module.py").read_text(encoding="utf-8") == "value = 1\n"
    assert [data for event_type, data in events if event_type == "diff"] == []


@pytest.mark.asyncio
async def test_write_file_replacement_still_emits_a_real_diff(
    config, store: Store, sample_repo: Path
):
    target = sample_repo / "replace_me.py"
    target.write_text("old = 1\n", encoding="utf-8")
    adapter = ScriptedAdapter(
        [
            outcome(
                tool_calls=[
                    tool_call(
                        "write_file",
                        {"path": "replace_me.py", "content": "new = 2\n"},
                    )
                ]
            ),
            outcome(text="Replaced the file."),
        ],
        config.providers["mock"],
    )
    loop = make_loop(config, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    events: list[tuple[str, dict]] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    result = await loop.run(
        session, "replace the file", emit=emit, permission_resolver=approve_all()
    )
    assert result.error is None
    diffs = [data for event_type, data in events if event_type == "diff"]
    assert len(diffs) == 1
    assert "-old = 1" in diffs[0]["patch"]
    assert "+new = 2" in diffs[0]["patch"]


@pytest.mark.asyncio
async def test_productive_step_budget_extends(config, store: Store, sample_repo: Path):
    config = config.model_copy(
        update={
            "agent": config.agent.model_copy(
                update={
                    "max_steps": 2,
                    "interactive_max_steps": 2,
                    "step_extension": 2,
                    "max_steps_ceiling": 4,
                    "self_check_min_steps": 99,
                }
            )
        }
    )
    adapter = ScriptedAdapter(
        [
            outcome(
                tool_calls=[
                    tool_call(
                        "todo",
                        {"items": [{"id": "a", "content": "work", "status": "in_progress"}]},
                    )
                ]
            ),
            outcome(
                tool_calls=[
                    tool_call(
                        "todo",
                        {"items": [{"id": "a", "content": "work", "status": "done"}]},
                    )
                ]
            ),
            outcome(text="Done."),
        ],
        config.providers["mock"],
    )
    loop = make_loop(config, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    events: list[tuple[str, dict]] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    result = await loop.run(
        session,
        "long task",
        emit=emit,
        permission_resolver=approve_all(),
        extend_budget=True,
    )
    assert result.error is None
    assert result.steps == 3
    assert any(event_type == "budget.extended" for event_type, _data in events)
    assert session_todos(store, session.id)[0]["status"] == "done"


@pytest.mark.asyncio
async def test_stalled_run_stops_before_ceiling(config, store: Store, sample_repo: Path):
    config = config.model_copy(
        update={
            "agent": config.agent.model_copy(
                update={
                    "max_steps": 5,
                    "interactive_max_steps": 5,
                    "step_extension": 5,
                    "max_steps_ceiling": 20,
                    "stall_limit": 2,
                }
            )
        }
    )
    adapter = ScriptedAdapter(
        [
            outcome(tool_calls=[tool_call("unknown_tool", {}, "call_a")]),
            outcome(tool_calls=[tool_call("unknown_tool", {}, "call_b")]),
        ],
        config.providers["mock"],
    )
    loop = make_loop(config, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    events: list[tuple[str, dict]] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    result = await loop.run(
        session,
        "stuck task",
        emit=emit,
        permission_resolver=approve_all(),
        extend_budget=True,
    )
    assert result.error == "stalled"
    assert result.steps == 2
    assert any(
        event_type == "error" and data.get("code") == "stalled" for event_type, data in events
    )


@pytest.mark.asyncio
async def test_todos_survive_across_runs_and_are_restored(config, store: Store, sample_repo: Path):
    config = config.model_copy(
        update={"agent": config.agent.model_copy(update={"self_check_min_steps": 99})}
    )
    adapter = ScriptedAdapter([outcome(text="Done.")], config.providers["mock"])
    loop = make_loop(config, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    store.update_session(
        session.id,
        metadata={
            "todos": [
                {"id": "a", "content": "first item", "status": "done"},
                {"id": "b", "content": "second item", "status": "done"},
            ]
        },
    )
    events: list[tuple[str, dict]] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    result = await loop.run(
        session,
        "continue",
        emit=emit,
        permission_resolver=approve_all(),
        extend_budget=True,
    )
    assert result.error is None
    system = "\n".join(
        message.content or ""
        for message in adapter.requests[0].messages
        if message.role == "system"
    )
    assert "Restored todo list" in system
    assert "2 todo items" in system
    assert any(event_type == "todo.update" for event_type, _data in events)
    todos = session_todos(store, session.id)
    assert [item["content"] for item in todos] == ["first item", "second item"]


@pytest.mark.asyncio
async def test_completion_gate_requires_todo_reconciliation(
    config, store: Store, sample_repo: Path
):
    config = config.model_copy(
        update={"agent": config.agent.model_copy(update={"self_check_min_steps": 99})}
    )
    adapter = ScriptedAdapter(
        [
            outcome(
                tool_calls=[
                    tool_call(
                        "todo",
                        {
                            "items": [
                                {"id": "a", "content": "done part", "status": "done"},
                                {"id": "b", "content": "report", "status": "in_progress"},
                            ]
                        },
                    )
                ]
            ),
            outcome(text="All done."),
            outcome(
                tool_calls=[
                    tool_call(
                        "todo",
                        {
                            "items": [
                                {"id": "a", "content": "done part", "status": "done"},
                                {"id": "b", "content": "report", "status": "done"},
                            ]
                        },
                    )
                ]
            ),
            outcome(text="All done."),
        ],
        config.providers["mock"],
    )
    loop = make_loop(config, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    events: list[tuple[str, dict]] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    result = await loop.run(
        session,
        "finish the report",
        emit=emit,
        permission_resolver=approve_all(),
        extend_budget=True,
    )
    assert result.error is None
    assert result.steps == 4
    assert any(
        event_type == "completion.check" and data.get("reason") == "todos"
        for event_type, data in events
    )
    assert "Completion check" in (adapter.requests[2].messages[-1].content or "")
    todos = session_todos(store, session.id)
    assert all(item["status"] == "done" for item in todos)


@pytest.mark.asyncio
async def test_completion_reconciles_last_in_progress_todo(config, store: Store, sample_repo: Path):
    config = config.model_copy(
        update={
            "agent": config.agent.model_copy(
                update={"self_check_min_steps": 99, "max_completion_nudges": 1}
            )
        }
    )
    adapter = ScriptedAdapter(
        [
            outcome(
                tool_calls=[
                    tool_call(
                        "todo",
                        {"items": [{"id": "a", "content": "report", "status": "in_progress"}]},
                    )
                ]
            ),
            outcome(text="Finished."),
            outcome(text="Finished."),
        ],
        config.providers["mock"],
    )
    loop = make_loop(config, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")

    async def emit(_event_type: str, _data: dict) -> None:
        return None

    result = await loop.run(
        session,
        "finish",
        emit=emit,
        permission_resolver=approve_all(),
        extend_budget=True,
    )
    assert result.error is None
    todos = session_todos(store, session.id)
    assert todos[0]["status"] == "done"


async def test_alternating_successful_reads_trigger_stall_guard(config, store, sample_repo):
    cfg = config.model_copy(update={"agent": config.agent.model_copy(update={"stall_limit": 3})})
    adapter = ScriptedAdapter(
        [
            outcome(
                tool_calls=[
                    tool_call(
                        "read_file",
                        {"path": "README.md" if index % 2 == 0 else "math_utils.py"},
                        f"read_{index}",
                    )
                ]
            )
            for index in range(5)
        ],
        cfg.providers["mock"],
    )
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    events = []

    async def emit(event, data):
        events.append((event, data))

    result = await make_loop(cfg, store, adapter).run(
        session, "Read the project", emit=emit, extend_budget=True
    )
    assert result.error == "stalled"
    assert result.steps == 5
    assert all(call["status"] == "ok" for call in store.get_tool_calls(session.id))
    assert any(event == "completion.check" and data["status"] == "stall" for event, data in events)


@pytest.mark.asyncio
async def test_repeated_identical_calls_do_not_extend_budget(
    config, store: Store, sample_repo: Path
):
    config = config.model_copy(
        update={
            "agent": config.agent.model_copy(
                update={
                    "max_steps": 2,
                    "interactive_max_steps": 2,
                    "step_extension": 10,
                    "max_steps_ceiling": 20,
                    "self_check_min_steps": 99,
                }
            )
        }
    )
    repeated = [tool_call("read_file", {"path": "README.md"}, f"call_{index}") for index in (1, 2)]
    adapter = ScriptedAdapter(
        [outcome(tool_calls=[call]) for call in repeated],
        config.providers["mock"],
    )
    loop = make_loop(config, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")

    async def emit(_event_type: str, _data: dict) -> None:
        return None

    result = await loop.run(
        session,
        "read the same file twice",
        emit=emit,
        permission_resolver=approve_all(),
        extend_budget=True,
    )
    assert result.error == "max_steps"
    assert result.steps == 2


@pytest.mark.asyncio
async def test_final_answer_does_not_trigger_extra_self_check(
    config, store: Store, sample_repo: Path
):
    config = config.model_copy(
        update={
            "agent": config.agent.model_copy(
                update={
                    "max_steps": 6,
                    "max_completion_nudges": 1,
                    "self_check_min_steps": 2,
                }
            )
        }
    )
    adapter = ScriptedAdapter(
        [
            outcome(
                tool_calls=[
                    tool_call(
                        "edit_file",
                        {
                            "path": "math_utils.py",
                            "old_string": "return a - b",
                            "new_string": "return a + b",
                        },
                    )
                ]
            ),
            outcome(text="Fixed the bug."),
            outcome(text="Fixed and verified; final answer."),
        ],
        config.providers["mock"],
    )
    loop = make_loop(config, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    events: list[tuple[str, dict]] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    result = await loop.run(
        session,
        "fix the bug",
        emit=emit,
        permission_resolver=approve_all(),
        extend_budget=True,
    )
    assert result.error is None
    assert result.steps == 2
    assert result.text == "Fixed the bug."
    assert not any(
        event_type == "completion.check" and data.get("reason") == "self_check"
        for event_type, data in events
    )


@pytest.mark.asyncio
async def test_memory_cue_nudges_memory_write(config, store: Store, sample_repo: Path):
    config = config.model_copy(
        update={
            "agent": config.agent.model_copy(
                update={"max_completion_nudges": 2, "self_check_min_steps": 99}
            )
        }
    )
    adapter = ScriptedAdapter(
        [
            outcome(text="I will remember that."),
            outcome(
                tool_calls=[
                    tool_call(
                        "memory_write",
                        {"target": "user", "content": "Always use pytest -q"},
                    )
                ]
            ),
            outcome(text="Remembered."),
        ],
        config.providers["mock"],
    )
    loop = make_loop(config, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    events: list[tuple[str, dict]] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    result = await loop.run(
        session,
        "ingat: always use pytest -q",
        emit=emit,
        permission_resolver=approve_all(),
        extend_budget=True,
    )
    assert result.error is None
    assert result.steps == 3
    assert any(
        event_type == "completion.check" and data.get("reason") == "memory"
        for event_type, data in events
    )
    assert "Always use pytest -q" in read_memory(sample_repo).user.content


@pytest.mark.asyncio
async def test_empty_final_answer_is_nudged(config, store: Store, sample_repo: Path):
    config = config.model_copy(
        update={
            "agent": config.agent.model_copy(
                update={"self_check_min_steps": 99, "max_completion_nudges": 1}
            )
        }
    )
    adapter = ScriptedAdapter(
        [outcome(), outcome(text="Done.")],
        config.providers["mock"],
    )
    loop = make_loop(config, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    events: list[tuple[str, dict]] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    result = await loop.run(
        session,
        "do the work",
        emit=emit,
        permission_resolver=approve_all(),
        extend_budget=True,
    )
    assert result.error is None
    assert result.steps == 2
    assert result.text == "Done."
    assert any(
        event_type == "completion.check" and data.get("reason") == "empty"
        for event_type, data in events
    )
