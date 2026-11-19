from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from siwah.config import Config
from siwah.core.checkpoints import CheckpointManager
from siwah.core.storage import Store
from siwah.server.runtime import ConfirmationRequired, DaemonRuntime


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_rewind_restores_exact_file_hashes(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    store = Store(tmp_path / "state.db")
    manager = CheckpointManager(
        workspace, "ses_checkpoint", store=store, root=tmp_path / "checkpoints"
    )
    first = manager.snapshot("initial", allow_empty=True)
    assert first is not None
    original = _hash(workspace / "a.py")

    (workspace / "a.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (workspace / "b.py").write_text("VALUE = 2\n", encoding="utf-8")
    second = manager.snapshot("second")
    assert second is not None

    (workspace / "a.py").write_text("def add(a, b):\n    return a * b\n", encoding="utf-8")
    record = store.get_checkpoint(first)
    assert record is not None
    assert manager.restore(record["commit_hash"]) is True
    assert _hash(workspace / "a.py") == original
    assert not (workspace / "b.py").exists()
    assert [item["label"] for item in store.list_checkpoints("ses_checkpoint")] == [
        "initial",
        "second",
    ]
    store.close()


def test_rewind_requires_confirmation(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_text("one\n", encoding="utf-8")
    store = Store(tmp_path / "state.db")
    runtime = DaemonRuntime(Config(), store=store)
    session = store.create_session(cwd=str(workspace), model=None)
    first = runtime.create_checkpoint(session.id, "one")
    (workspace / "a.txt").write_text("two\n", encoding="utf-8")
    runtime.create_checkpoint(session.id, "two")

    with pytest.raises(ConfirmationRequired):
        runtime.rewind(session.id, checkpoint_id=first["id"], confirm=False)
    runtime.rewind(session.id, checkpoint_id=first["id"], confirm=True)
    assert (workspace / "a.txt").read_text() == "one\n"
    store.close()


@pytest.mark.asyncio
async def test_loop_snapshots_writes_and_risky_bash(config, store: Store, sample_repo: Path):
    from siwah.config import CheckpointConfig
    from siwah.permissions.policy import Approval

    from .conftest import ScriptedAdapter, make_loop, outcome, tool_call

    cfg = config.model_copy(update={"checkpoints": CheckpointConfig(enabled=True)})
    adapter = ScriptedAdapter(
        [
            outcome(
                tool_calls=[
                    tool_call(
                        "write_file",
                        {"path": "note.txt", "content": "checkpointed\n"},
                        "call_write",
                    )
                ]
            ),
            outcome(tool_calls=[tool_call("bash", {"command": "rm -rf nonexistent"}, "call_bash")]),
            outcome(text="Done."),
        ],
        cfg.providers["mock"],
    )
    loop = make_loop(cfg, store, adapter)
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")

    async def approve(_request, _request_id):
        return Approval(decision="once")

    async def emit(_event_type: str, _data: dict) -> None:
        return None

    result = await loop.run(session, "Change files", emit=emit, permission_resolver=approve)
    assert result.error is None
    labels = [item["label"] for item in store.list_checkpoints(session.id)]
    assert "initial" in labels
    assert any(label and label.startswith("write_file:") for label in labels)
    assert "before risky bash" in labels
    assert (sample_repo / "note.txt").read_text() == "checkpointed\n"
