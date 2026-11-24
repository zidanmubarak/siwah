from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from siwah.hooks import HookManager

from .conftest import ScriptedAdapter, make_loop, outcome, tool_call


def _write_hook(root: Path, name: str, payload: dict) -> None:
    directory = root / "harness" / "hooks"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.asyncio
async def test_hook_manager_contract(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path))
    deny = (
        "import json,sys; d=json.load(sys.stdin); "
        "print(json.dumps({'allow': False, 'reason': 'blocked ' + d['tool']}))"
    )
    context = "import json; print(json.dumps({'context': 'hook context'}))"
    _write_hook(
        tmp_path,
        "deny",
        {
            "event": "PreToolUse",
            "matcher": "read_file",
            "command": [sys.executable, "-c", deny],
        },
    )
    _write_hook(
        tmp_path,
        "post",
        {"event": "PostToolUse", "command": [sys.executable, "-c", context]},
    )
    manager = HookManager.load()

    blocked = await manager.run("PreToolUse", {"tool": "read_file"})
    assert blocked.allow is False
    assert "blocked read_file" in (blocked.reason or "")

    post = await manager.run("PostToolUse", {"tool": "read_file", "ok": True})
    assert post.context == "hook context"


@pytest.mark.asyncio
async def test_pre_tool_hook_blocks_a_tool_call(config, store, sample_repo: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path_home(store)))
    deny = "import json,sys; json.load(sys.stdin); print(json.dumps({'allow': False, 'reason': 'blocked'}))"
    _write_hook(
        tmp_path_home(store),
        "deny",
        {"event": "PreToolUse", "matcher": "read_file", "command": [sys.executable, "-c", deny]},
    )
    adapter = ScriptedAdapter(
        [
            outcome(tool_calls=[tool_call("read_file", {"path": "README.md"})]),
            outcome(text="done"),
        ],
        config.providers["mock"],
    )
    loop = make_loop(config, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")

    async def emit(_event: str, _data: dict) -> None:
        return None

    result = await loop.run(session, "read", emit=emit, max_steps=3)
    calls = store.get_tool_calls(session.id)

    assert result.text == "done"
    assert calls[0]["status"] == "error"
    assert "blocked" in calls[0]["result"]["error"]


async def test_completion_reports_pending_hooks(config, store, sample_repo, monkeypatch):
    home = tmp_path_home(store)
    monkeypatch.setenv("SIWAH_HOME", str(home))
    marker = home / "lifecycle"
    for event in ("SessionEnd", "Stop"):
        script = (
            "from pathlib import Path; import sys; "
            "with_path = Path(sys.argv[1]); "
            "with_path.write_text((with_path.read_text() if with_path.exists() else '') + sys.argv[2])"
        )
        _write_hook(
            home,
            event,
            {"event": event, "command": [sys.executable, "-c", script, str(marker), event]},
        )
    adapter = ScriptedAdapter([outcome(text="Done.")], config.providers["mock"])
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    saw_phase = False

    async def emit(event, data):
        nonlocal saw_phase
        if event == "status" and data.get("phase") == "finishing_hooks":
            saw_phase = True
            assert not marker.exists()
        if event == "run.end":
            assert marker.read_text() == "SessionEndStop"

    result = await make_loop(config, store, adapter).run(session, "Finish", emit=emit)
    assert result.error is None
    assert saw_phase


def tmp_path_home(store) -> Path:
    return Path(store.path).parent / "home"
