from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from siwah.cli import main as cli_main
from siwah.kernel.manifest import ManifestStatus, verify_manifest, write_manifest
from siwah.kernel.snapshot import (
    SnapshotResult,
    create_healthy_snapshot,
    rollback_to_tag,
)
from siwah.kernel.supervisor import BootResult, RuntimeEndpoint, Supervisor


def _status(ok: bool, digest: str = "a" * 64) -> ManifestStatus:
    return ManifestStatus(
        present=True,
        ok=ok,
        expected_hash=digest,
        actual_hash=digest if ok else "b" * 64,
        changed=[] if ok else ["src/siwah/kernel/guard.py"],
    )


def _audit(events: list[tuple[str, dict[str, Any]]]):
    def write(event: str, data: dict[str, Any]) -> dict[str, Any]:
        events.append((event, data))
        return {"event": event, "data": data}

    return write


def test_manifest_mismatch_rolls_back_before_boot(tmp_path: Path):
    events: list[tuple[str, dict[str, Any]]] = []
    statuses = iter([_status(False), _status(True), _status(True)])
    rolled: list[Path] = []

    def rollback(repo: Path) -> SnapshotResult:
        rolled.append(repo)
        return SnapshotResult(True, "kernel-healthy-aaaa", "restored")

    supervisor = Supervisor(
        tmp_path,
        boot=lambda _on_healthy: BootResult(True, 0),
        rollback=rollback,
        verify=lambda _repo: next(statuses),
        snapshot=lambda _repo, _hash: None,
        audit=_audit(events),
    )

    assert supervisor.run() == 0
    assert rolled == [tmp_path.resolve()]
    names = [event for event, _ in events]
    assert "manifest.mismatch" in names
    assert "rollback.manifest" in names
    assert "manifest.recovered" in names


def test_two_failed_boots_trigger_auto_rollback(tmp_path: Path):
    events: list[tuple[str, dict[str, Any]]] = []
    boots = iter([BootResult(False, 1, "boom"), BootResult(False, 1, "boom")])
    rolled: list[Path] = []

    def rollback(repo: Path) -> SnapshotResult:
        rolled.append(repo)
        return SnapshotResult(True, "kernel-healthy-aaaa", "restored")

    supervisor = Supervisor(
        tmp_path,
        boot=lambda _on_healthy: next(boots),
        rollback=rollback,
        verify=lambda _repo: _status(True),
        snapshot=lambda _repo, _hash: None,
        audit=_audit(events),
    )

    assert supervisor.run() == 1
    assert rolled == [tmp_path.resolve()]
    assert any(event == "rollback.auto" for event, _ in events)
    failed = [data for event, data in events if event == "boot.failed"]
    assert [item["failures"] for item in failed] == [1, 2]


def test_clean_boot_creates_healthy_snapshot(tmp_path: Path):
    events: list[tuple[str, dict[str, Any]]] = []
    tags: list[tuple[Path, str]] = []

    def snapshot(repo: Path, digest: str) -> str:
        tags.append((repo, digest))
        return "kernel-healthy-aaaa"

    def boot(on_healthy):
        on_healthy()
        return BootResult(True, 0)

    supervisor = Supervisor(
        tmp_path,
        boot=boot,
        rollback=lambda _repo: SnapshotResult(False, None, "should not run"),
        verify=lambda _repo: _status(True),
        snapshot=snapshot,
        audit=_audit(events),
    )

    assert supervisor.run() == 0
    assert tags == [(tmp_path.resolve(), "a" * 64)]
    assert any(event == "snapshot.created" for event, _ in events)


def test_failed_rollback_prevents_boot(tmp_path: Path):
    events: list[tuple[str, dict[str, Any]]] = []
    boots: list[BootResult] = []

    supervisor = Supervisor(
        tmp_path,
        boot=lambda _on_healthy: boots.append(BootResult(True, 0)) or BootResult(True, 0),
        rollback=lambda _repo: SnapshotResult(False, None, "no tag"),
        verify=lambda _repo: _status(False),
        snapshot=lambda _repo, _hash: None,
        audit=_audit(events),
    )

    assert supervisor.run() == 1
    assert boots == []
    assert any(
        event == "manifest.revert_failed" or event == "rollback.manifest" for event, _ in events
    )


def test_one_failure_then_success_does_not_rollback(tmp_path: Path):
    boots = iter([BootResult(False, 1, "boom"), BootResult(True, 0)])
    supervisor = Supervisor(
        tmp_path,
        boot=lambda _on_healthy: next(boots),
        rollback=lambda _repo: SnapshotResult(False, None, "should not run"),
        verify=lambda _repo: _status(True),
        snapshot=lambda _repo, _hash: None,
        audit=lambda _event, _data: {},
    )
    assert supervisor.run() == 0


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_manifest_mismatch_restores_real_files(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "src" / "siwah" / "kernel").mkdir(parents=True)
    (repo / "evals").mkdir(parents=True)
    kernel_file = repo / "src" / "siwah" / "kernel" / "guard.py"
    kernel_file.write_text("original\n", encoding="utf-8")
    (repo / "evals" / "task.json").write_text("{}\n", encoding="utf-8")
    write_manifest(repo)

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)

    git("init")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    git("add", ".")
    git("commit", "-m", "initial")
    digest = verify_manifest(repo).actual_hash
    assert digest is not None
    assert create_healthy_snapshot(repo, digest) is not None

    kernel_file.write_text("corrupt\n", encoding="utf-8")
    assert not verify_manifest(repo).ok

    supervisor = Supervisor(
        repo,
        boot=lambda _on_healthy: BootResult(True, 0),
        rollback=rollback_to_tag,
        verify=verify_manifest,
        snapshot=lambda _repo, _hash: None,
        audit=lambda _event, _data: {},
    )

    assert supervisor.run() == 0
    assert kernel_file.read_text(encoding="utf-8") == "original\n"
    assert verify_manifest(repo).ok


def test_ensure_daemon_starts_the_supervisor(tmp_path: Path, monkeypatch):
    calls: list[list[str]] = []

    class FakeProcess:
        def __init__(self, args, **_kwargs):
            calls.append(list(args))

        def poll(self):
            return None

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return 0

    counter = {"value": 0}

    def endpoint():
        counter["value"] += 1
        if counter["value"] == 1:
            return None
        return RuntimeEndpoint("http://127.0.0.1:9999", "token")

    monkeypatch.setattr(cli_main, "runtime_endpoint", endpoint)
    monkeypatch.setattr(cli_main, "runtime_healthy", lambda _endpoint: True)
    monkeypatch.setattr(cli_main.subprocess, "Popen", FakeProcess)

    server, token, process = cli_main._ensure_daemon()
    assert server == "http://127.0.0.1:9999"
    assert token == "token"
    assert process is not None
    assert calls and calls[0][-1] == "supervise"


def test_runtime_endpoint_requires_the_supervisor_nonce_and_pid(tmp_path, monkeypatch):
    import json

    from siwah.kernel.supervisor import runtime_endpoint
    from siwah.paths import runtime_path

    monkeypatch.setenv("SIWAH_HOME", str(tmp_path))
    runtime_path().write_text(
        json.dumps(
            {
                "host": "127.0.0.1",
                "port": 9999,
                "token": "file-token",
                "pid": 1234,
            }
        ),
        encoding="utf-8",
    )

    endpoint = runtime_endpoint(expected_pid=1234, token="supervisor-token")
    assert endpoint is not None
    assert endpoint.token == "supervisor-token"
    assert endpoint.server == "http://127.0.0.1:9999"
    assert runtime_endpoint(expected_pid=9999) is None


def test_runtime_healthy_requires_the_nonce_proof():
    import hashlib
    import hmac
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from siwah.kernel.supervisor import RuntimeEndpoint, runtime_healthy

    nonce = "supervisor-nonce"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            challenge = self.headers.get("x-siwah-challenge", "")
            proof = hmac.new(nonce.encode(), challenge.encode(), hashlib.sha256).hexdigest()
            self.send_response(200)
            self.send_header("X-Siwah-Proof", proof)
            self.end_headers()

        def log_message(self, _format: str, *_args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        endpoint = RuntimeEndpoint(f"http://127.0.0.1:{port}", "token")
        assert runtime_healthy(endpoint, nonce=nonce)
        assert not runtime_healthy(endpoint, nonce="wrong-nonce")
    finally:
        server.shutdown()
        server.server_close()


def test_boot_accepts_a_windows_launcher_pid_that_differs_from_the_daemon(monkeypatch):
    from siwah.kernel import supervisor

    class FakeProcess:
        pid = 100
        returncode = 0

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def terminate(self) -> None:
            return

    healthy: list[bool] = []
    killed: list[int] = []
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    monkeypatch.setattr(
        supervisor,
        "runtime_endpoint",
        lambda **_kwargs: RuntimeEndpoint("http://127.0.0.1:9", "token"),
    )
    monkeypatch.setattr(supervisor, "runtime_healthy", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(supervisor, "read_runtime", lambda: {"pid": 200})
    monkeypatch.setattr(supervisor.os, "kill", lambda pid, _sig: killed.append(pid))

    result = supervisor.boot_daemon_once(lambda: healthy.append(True), timeout_s=1)

    assert result.healthy is True
    assert healthy == [True]
    assert killed == [200]
