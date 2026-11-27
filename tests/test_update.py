from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from siwah import update as updater
from siwah.config import Config
from siwah.server.app import create_app
from siwah.update import (
    UpdateApplyResult,
    UpdateStatus,
    apply_update,
    check_for_update,
    version_key,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _pyproject(version: str) -> str:
    return (
        "[project]\n"
        'name = "siwah"\n'
        f'version = "{version}"\n'
        'description = "test checkout"\n'
        'requires-python = ">=3.11"\n'
    )


def _repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "-b", "master")
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-b", "master")
    _git(upstream, "config", "user.email", "test@example.invalid")
    _git(upstream, "config", "user.name", "Test")
    (upstream / "pyproject.toml").write_text(_pyproject("0.1.0"), encoding="utf-8")
    (upstream / "README.md").write_text("one\n", encoding="utf-8")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-m", "initial")
    _git(upstream, "remote", "add", "origin", str(remote))
    _git(upstream, "push", "-u", "origin", "master")

    work = tmp_path / "work"
    _git(tmp_path, "clone", "--branch", "master", str(remote), str(work))
    _git(work, "config", "user.email", "test@example.invalid")
    _git(work, "config", "user.name", "Test")
    return upstream, work


def _bump(upstream: Path, version: str) -> None:
    (upstream / "pyproject.toml").write_text(_pyproject(version), encoding="utf-8")
    (upstream / "README.md").write_text(f"{version}\n", encoding="utf-8")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-m", f"release {version}")
    _git(upstream, "push", "origin", "master")


def _key(value: str) -> tuple[int, int, int, int, str]:
    key = version_key(value)
    assert key is not None
    return key


def test_version_key_orders_releases_and_prereleases():
    assert _key("0.2.0") > _key("0.1.9")
    assert _key("v1.0.0") > _key("0.9.9")
    assert _key("1.0.0") > _key("1.0.0-rc1")
    assert version_key("not-a-version") is None


def test_check_and_apply_update_from_the_upstream_branch(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    upstream, work = _repo_with_remote(tmp_path)
    _bump(upstream, "9.9.9")

    status = check_for_update(refresh=True, root=work)
    assert status.latest == "9.9.9"
    assert status.available is True
    assert status.commits_behind == 1
    assert status.can_apply is True
    assert status.error is None

    monkeypatch.setattr(
        updater,
        "_reinstall_tool",
        lambda _root: (True, ["Reinstalled Python dependencies."]),
    )
    result = apply_update(root=work, rebuild_tui=False)
    assert result.ok is True
    assert result.from_version == "0.1.0"
    assert result.to_version == "9.9.9"
    assert result.restart_required is True
    assert (work / "pyproject.toml").read_text(encoding="utf-8").count("9.9.9") == 1


def test_release_tag_wins_over_an_unreleased_branch_version(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    upstream, work = _repo_with_remote(tmp_path)
    _git(upstream, "tag", "-a", "v99.0.0", "-m", "siwah 99.0.0")
    _git(upstream, "push", "origin", "v99.0.0")
    _bump(upstream, "9.9.9")

    status = check_for_update(refresh=True, root=work)
    assert status.latest == "99.0.0"
    assert status.source == "tag"
    assert status.available is True


def test_apply_refuses_a_dirty_checkout(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    upstream, work = _repo_with_remote(tmp_path)
    _bump(upstream, "9.9.9")
    (work / "README.md").write_text("local change\n", encoding="utf-8")

    result = apply_update(root=work, rebuild_tui=False)
    assert result.ok is False
    assert "local changes" in result.detail


@pytest.mark.parametrize("succeeds", [True, False])
def test_reinstall_tool_uses_editable_install(tmp_path: Path, monkeypatch, succeeds: bool):
    calls: list[list[str]] = []

    def run(command, *, cwd, timeout, env=None):
        calls.append(command)
        return succeeds, "output"

    monkeypatch.setattr(updater, "_find_uv", lambda _root: "uv")
    monkeypatch.setattr(updater, "_run", run)
    ok, steps = updater._reinstall_tool(tmp_path)
    assert ok is succeeds
    assert calls and calls[0][:4] == ["uv", "pip", "install", "--python"]
    assert "--editable" in calls[0]
    assert str(tmp_path) in calls[0]
    assert any("Python dependency install failed" in step for step in steps) is not succeeds


@pytest.mark.parametrize("succeeds", [True, False])
def test_rebuild_stages_only_complete_binary(tmp_path: Path, monkeypatch, succeeds: bool):
    tui = tmp_path / "source" / "ui" / "tui"
    tui.mkdir(parents=True)
    (tui / "package.json").touch()
    home = tmp_path / "home"
    bin_dir = home / "bin"
    bin_dir.mkdir(parents=True)
    name = "siwah-tui.exe" if os.name == "nt" else "siwah-tui"
    active = bin_dir / name
    staged = bin_dir / f"{name}.next"
    active.write_bytes(b"active")
    staged.write_bytes(b"previous stage")

    def run(command, *, cwd, timeout, env=None):
        if command[1] == "install":
            return True, ""
        assert env is not None
        built = Path(env["SIWAH_TUI_OUTFILE"])
        assert built.parent.parent == bin_dir
        built.write_bytes(b"updated")
        return succeeds, "build output"

    monkeypatch.setenv("SIWAH_HOME", str(home))
    monkeypatch.setattr(updater, "_find_bun", lambda _root: "bun")
    monkeypatch.setattr(updater, "_run", run)

    steps = updater._rebuild_tui(tmp_path / "source")

    assert active.read_bytes() == b"active"
    assert not (tui / "dist").exists()
    assert sorted(bin_dir.iterdir()) == sorted([active, staged])
    assert staged.read_bytes() == (b"updated" if succeeds else b"previous stage")
    assert any("TUI build failed" in step for step in steps) is not succeeds


@pytest.mark.asyncio
async def test_update_endpoint_reports_the_check(monkeypatch):
    from siwah.server import app as server_app

    monkeypatch.setattr(
        server_app,
        "check_for_update",
        lambda **_kwargs: UpdateStatus(
            current="0.2.0", latest="0.2.1", available=True, can_apply=True
        ),
    )
    app = create_app(Config(), runtime=None, token="test-token")
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/update", headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 200
    assert response.json()["latest"] == "0.2.1"
    assert response.json()["available"] is True


def test_update_endpoint_returns_conflict_on_failed_apply(monkeypatch):
    from siwah.server import app as server_app

    monkeypatch.setattr(
        server_app,
        "apply_update",
        lambda: UpdateApplyResult(ok=False, from_version="0.2.0", detail="not a checkout"),
    )
    app = create_app(Config(), runtime=None, token="test-token")

    async def run() -> httpx.Response:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/v1/update", headers={"Authorization": "Bearer test-token"})

    import asyncio

    response = asyncio.run(run())
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "update_failed"
