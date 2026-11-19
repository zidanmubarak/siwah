from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from siwah.config import SandboxConfig
from siwah.sandbox import Sandbox


@pytest.mark.asyncio
async def test_local_sandbox_runs_a_command(tmp_path: Path):
    sandbox = Sandbox(SandboxConfig(backend="local"), cwd=tmp_path)
    result = await sandbox.run([sys.executable, "-c", "print('ok')"])
    assert result.ok
    assert result.backend == "local"
    assert "ok" in result.output


def test_docker_backend_requires_an_image(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        "siwah.sandbox.shutil.which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    without_image = Sandbox(SandboxConfig(image=""), cwd=tmp_path)
    assert without_image.backend() == "local"

    with_image = Sandbox(SandboxConfig(image="sandbox:latest"), cwd=tmp_path)
    assert with_image.backend() == "docker"


@pytest.mark.asyncio
async def test_explicit_docker_backend_requires_an_image(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        "siwah.sandbox.shutil.which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    sandbox = Sandbox(SandboxConfig(backend="docker", image=""), cwd=tmp_path)
    result = await sandbox.run(["sh", "-lc", "echo should-not-run"])
    assert not result.ok
    assert result.backend == "docker"
    assert "requires [sandbox].image" in result.output


@pytest.mark.asyncio
async def test_local_sandbox_timeout_kills_the_command(tmp_path: Path):
    sandbox = Sandbox(SandboxConfig(backend="local"), cwd=tmp_path)
    started = time.monotonic()
    result = await sandbox.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout_s=0.2)
    elapsed = time.monotonic() - started
    assert not result.ok
    assert "timed out" in result.output
    assert elapsed < 5


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("SIWAH_TEST_DOCKER") != "1",
    reason="set SIWAH_TEST_DOCKER=1 to run the Docker integration test",
)
async def test_docker_sandbox_uses_the_linux_shell_and_workspace(tmp_path: Path):
    image = os.environ.get("SIWAH_TEST_DOCKER_IMAGE", "alpine:3.20")
    (tmp_path / "marker.txt").write_text("visible\n", encoding="utf-8")
    sandbox = Sandbox(SandboxConfig(backend="docker", image=image), cwd=tmp_path)
    result = await sandbox.shell("pwd && cat marker.txt")
    assert result.ok, result.output
    assert result.backend == "docker"
    assert "/workspace" in result.output
    assert "visible" in result.output


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("SIWAH_TEST_DOCKER") != "1",
    reason="set SIWAH_TEST_DOCKER=1 to run the Docker integration test",
)
async def test_docker_sandbox_timeout_removes_the_container(tmp_path: Path):
    image = os.environ.get("SIWAH_TEST_DOCKER_IMAGE", "alpine:3.20")
    sandbox = Sandbox(SandboxConfig(backend="docker", image=image), cwd=tmp_path)
    result = await sandbox.shell("sleep 30", timeout_s=1)
    assert not result.ok
    assert "timed out" in result.output
    listing = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=siwah-sandbox-", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert f"-{os.getpid()}-" not in listing
