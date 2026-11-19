from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from siwah.config import CheckpointConfig
from siwah.core.storage import Store
from siwah.server.app import create_app
from siwah.server.runtime import DaemonRuntime


@pytest.mark.asyncio
async def test_phase4_checkpoint_rewind_compact_plan_and_tool_output(
    config, store: Store, sample_repo: Path
):
    cfg = config.model_copy(update={"checkpoints": CheckpointConfig(enabled=True)})
    runtime = DaemonRuntime(cfg, store=store)
    app = create_app(cfg, runtime=runtime, token="test-token")
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": "Bearer test-token"}
        meta = await client.get("/v1/meta", headers=headers)
        assert meta.status_code == 200
        assert "checkpoints" in meta.json()["capabilities"]["features"]

        created = await client.post(
            "/v1/sessions",
            headers=headers,
            json={"cwd": str(sample_repo), "model": "mock/model-a"},
        )
        assert created.status_code == 200, created.text
        session_id = created.json()["id"]

        checkpoint = await client.post(
            f"/v1/sessions/{session_id}/checkpoints",
            headers=headers,
            json={"label": "before edit"},
        )
        assert checkpoint.status_code == 200, checkpoint.text
        checkpoint_id = checkpoint.json()["id"]
        commit = checkpoint.json()["commit"]
        assert commit
        original_hash = hashlib.sha256((sample_repo / "math_utils.py").read_bytes()).hexdigest()

        (sample_repo / "math_utils.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8"
        )
        listed = await client.get(f"/v1/sessions/{session_id}/checkpoints", headers=headers)
        assert listed.status_code == 200
        assert any(item["id"] == checkpoint_id for item in listed.json()["checkpoints"])

        blocked = await client.post(
            f"/v1/sessions/{session_id}/rewind",
            headers=headers,
            json={"checkpoint_id": checkpoint_id, "confirm": False},
        )
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "confirmation_required"

        restored = await client.post(
            f"/v1/sessions/{session_id}/rewind",
            headers=headers,
            json={"checkpoint_id": checkpoint_id, "confirm": True},
        )
        assert restored.status_code == 200, restored.text
        assert (
            hashlib.sha256((sample_repo / "math_utils.py").read_bytes()).hexdigest()
            == original_hash
        )

        compacted = await client.post(f"/v1/sessions/{session_id}/compact", headers=headers)
        assert compacted.status_code == 200, compacted.text
        assert "Goal" in compacted.json()["summary"]

        plan_path = sample_repo / ".siwah" / "plan.md"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text("# Plan\n\n1. Fix add\n", encoding="utf-8")
        plan = await client.get(f"/v1/sessions/{session_id}/plan", headers=headers)
        assert plan.status_code == 200
        assert plan.json()["exists"] is True
        assert "Fix add" in plan.json()["content"]

        store.record_tool_call(
            session_id=session_id,
            tool_call_id="call_output",
            message_id=None,
            name="read_file",
            arguments={},
            result={
                "ok": True,
                "output": "summary",
                "data": {"handle": "call_output", "full_output": "full output"},
            },
            status="ok",
        )
        output = await client.get(
            f"/v1/sessions/{session_id}/tool_calls/call_output", headers=headers
        )
        assert output.status_code == 200, output.text
        assert output.json()["output"] == "full output"

        (sample_repo / "math_utils.py").write_text("changed\n", encoding="utf-8")
        forked = await client.post(
            f"/v1/sessions/{session_id}/fork",
            headers=headers,
            json={"checkpoint_id": checkpoint_id},
        )
        assert forked.status_code == 200, forked.text
        assert forked.json()["session"]["id"] != session_id
        assert (sample_repo / "math_utils.py").read_text() == ("def add(a, b):\n    return a - b\n")
