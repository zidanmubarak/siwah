from __future__ import annotations

import hashlib
import subprocess
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


async def test_changes_compares_final_turn_snapshot_and_requires_auth(config, store, sample_repo):
    runtime = DaemonRuntime(config, store=store)
    app = create_app(config, runtime=runtime, token="test-token")
    session = store.create_session(cwd=str(sample_repo), model="mock/model-a")
    (sample_repo / "preexisting.txt").write_text("local work", encoding="utf-8")
    base = runtime.create_checkpoint(session.id, "initial")
    target = sample_repo / "example.txt"
    target.write_text("intermediate", encoding="utf-8")
    runtime.create_checkpoint(session.id, "write_file:first")
    target.write_text("final output", encoding="utf-8")
    runtime.create_checkpoint(session.id, "edit_file:second")
    runtime.create_checkpoint(session.id, "initial")
    url = f"/v1/sessions/{session.id}/changes"
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get(url)).status_code == 401
        headers = {"Authorization": "Bearer test-token"}
        listing = await client.get(url, params={"checkpoint_id": base["id"]}, headers=headers)
        assert listing.status_code == 200, listing.text
        assert [(entry["path"], entry["patch"]) for entry in listing.json()["files"]] == [
            ("example.txt", ""),
        ]
        response = await client.get(
            url,
            params={
                "checkpoint_id": base["id"],
                "file_path": "example.txt",
                "end_checkpoint_id": listing.json()["end"],
            },
            headers=headers,
        )
    assert response.status_code == 200, response.text
    files = response.json()["files"]
    assert [file["path"] for file in files] == ["example.txt"]
    assert "+final output" in files[0]["patch"]
    assert "intermediate" not in files[0]["patch"]


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "initial",
    )


async def _workspace_changes(config, store: Store, repo: Path) -> httpx.Response:
    runtime = DaemonRuntime(config, store=store)
    app = create_app(config, runtime=runtime, token="test-token")
    session = store.create_session(cwd=str(repo), model="mock/model-a")
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(
            f"/v1/sessions/{session.id}/changes",
            params={"scope": "workspace"},
            headers={"Authorization": "Bearer test-token"},
        )


@pytest.mark.asyncio
async def test_workspace_changes_list_modified_and_untracked_files(config, store, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# old\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("old\n", encoding="utf-8")
    _init_repo(repo)
    (repo / "README.md").write_text("# changed\n", encoding="utf-8")
    (repo / "new.txt").write_text("new\n", encoding="utf-8")

    response = await _workspace_changes(config, store, repo)
    assert response.status_code == 200, response.text
    files = {entry["path"]: entry for entry in response.json()["files"]}
    assert set(files) == {"README.md", "new.txt"}
    assert files["new.txt"]["status"] == "?"
    assert files["README.md"]["patch"] == ""

    runtime = DaemonRuntime(config, store=store)
    app = create_app(config, runtime=runtime, token="test-token")
    session = store.create_session(cwd=str(repo), model="mock/model-a")
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        detail = await client.get(
            f"/v1/sessions/{session.id}/changes",
            params={"scope": "workspace", "file_path": "README.md"},
            headers={"Authorization": "Bearer test-token"},
        )
    assert detail.status_code == 200, detail.text
    assert "+# changed" in detail.json()["files"][0]["patch"]


@pytest.mark.asyncio
async def test_workspace_changes_support_a_repository_subdirectory(config, store, tmp_path):
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    (repo / "sub" / "file.txt").write_text("old\n", encoding="utf-8")
    _init_repo(repo)
    (repo / "sub" / "file.txt").write_text("new\n", encoding="utf-8")

    response = await _workspace_changes(config, store, repo / "sub")
    assert response.status_code == 200, response.text
    assert [(entry["path"], entry["status"]) for entry in response.json()["files"]] == [
        ("file.txt", "M")
    ]


@pytest.mark.asyncio
async def test_workspace_changes_support_unborn_branch(config, store, tmp_path):
    repo = tmp_path / "fresh"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "new.txt").write_text("hello\n", encoding="utf-8")

    response = await _workspace_changes(config, store, repo)
    assert response.status_code == 200, response.text
    assert [entry["path"] for entry in response.json()["files"]] == ["new.txt"]
    assert response.json()["available"] is True


@pytest.mark.asyncio
async def test_workspace_changes_reject_gitfile_outside_workspace(
    config, store, tmp_path, monkeypatch
):
    other = tmp_path / "other"
    other.mkdir()
    (other / "secret.txt").write_text("TOP SECRET\n", encoding="utf-8")
    _init_repo(other)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").write_text(f"gitdir: {other / '.git'}\n", encoding="utf-8")

    response = await _workspace_changes(config, store, workspace)
    assert response.status_code == 400
    assert "TOP SECRET" not in response.text

    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    response = await _workspace_changes(config, store, workspace)
    assert response.status_code == 400
    assert "TOP SECRET" not in response.text
