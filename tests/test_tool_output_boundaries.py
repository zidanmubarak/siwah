from __future__ import annotations

from pathlib import Path

import pytest

from siwah.core.context import read_tool_output, write_tool_output
from siwah.core.storage import Store
from siwah.tools.output import ToolOutputTool

from .test_context import _context


def test_output_identifiers_cannot_escape_artifact_directory(store, tmp_path: Path):
    outside = tmp_path / "outside"
    outside.with_suffix(".txt").write_text("must not read", encoding="utf-8")
    for handle in [str(outside), "../../outside", r"..\outside", "C:outside", "a/b"]:
        assert read_tool_output("ses_test", handle) is None
        with pytest.raises(ValueError):
            write_tool_output("ses_test", handle, "must not overwrite")
    assert outside.with_suffix(".txt").read_text(encoding="utf-8") == "must not read"
    with pytest.raises(ValueError):
        write_tool_output("../escape", "call_safe", "must not write")


@pytest.mark.asyncio
async def test_output_handle_cannot_reopen_another_sessions_record(store: Store, sample_repo):
    first = store.create_session(cwd=str(sample_repo), model=None)
    second = store.create_session(cwd=str(sample_repo), model=None)
    store.record_tool_call(
        session_id=first.id,
        tool_call_id="call_shared",
        message_id=None,
        name="read_file",
        arguments={},
        result={"ok": True, "output": "first session private output"},
        status="ok",
    )
    context = _context(sample_repo, store=store)
    context.session_id = second.id
    result = await ToolOutputTool().run(context, handle="call_shared")
    assert not result.ok
    assert "first session private output" not in result.output


def test_valid_output_handles_still_round_trip(store):
    write_tool_output("ses_test", "call_test", "saved output")
    assert read_tool_output("ses_test", "call_test") == "saved output"


def test_offloaded_output_is_redacted_on_disk(store):
    secret = "sk-SYNTHETIC_OFFLOAD_SECRET_0123456789"
    path = write_tool_output("ses_test", "call_test", f"Output: {secret}")
    assert secret not in path.read_text(encoding="utf-8")
    assert "[REDACTED]" in path.read_text(encoding="utf-8")
