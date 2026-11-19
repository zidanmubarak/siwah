"""Supervisor: verify the kernel, boot the daemon, and recover on failure."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import signal
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audit import audit_event
from .manifest import ManifestStatus, verify_manifest
from .paths import ensure_home, runtime_path, siwah_home
from .snapshot import (
    SnapshotResult,
    create_healthy_snapshot,
    is_trusted_snapshot,
    latest_healthy_tag,
    protected_changes,
    rollback_to_tag,
)


@dataclass(frozen=True)
class BootResult:
    healthy: bool
    exit_code: int = 0
    error: str | None = None


RUNTIME_TOKEN_ENV = "SIWAH_RUNTIME_TOKEN"
SUPERVISOR_NONCE_ENV = "SIWAH_SUPERVISOR_NONCE"


@dataclass(frozen=True)
class RuntimeEndpoint:
    server: str
    token: str


def read_runtime() -> dict[str, Any] | None:
    try:
        data = json.loads(runtime_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def runtime_endpoint(
    *,
    expected_pid: int | None = None,
    token: str | None = None,
) -> RuntimeEndpoint | None:
    data = read_runtime()
    if data is None:
        return None
    if expected_pid is not None and data.get("pid") != expected_pid:
        return None
    host = str(data.get("host", ""))
    port = data.get("port")
    resolved_token = token or str(data.get("token", ""))
    if not host or not isinstance(port, int) or not resolved_token:
        return None
    client_host = "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host
    return RuntimeEndpoint(f"http://{client_host}:{port}", resolved_token)


def runtime_healthy(
    endpoint: RuntimeEndpoint, *, timeout_s: float = 0.5, nonce: str | None = None
) -> bool:
    headers = {"Authorization": f"Bearer {endpoint.token}"}
    challenge = ""
    if nonce:
        challenge = secrets.token_hex(16)
        headers["X-Siwah-Challenge"] = challenge
    request = urllib.request.Request(f"{endpoint.server.rstrip('/')}/v1/health", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            if response.status != 200:
                return False
            if nonce:
                proof = response.headers.get("X-Siwah-Proof", "")
                expected = hmac.new(nonce.encode(), challenge.encode(), hashlib.sha256).hexdigest()
                return hmac.compare_digest(proof, expected)
            return True
    except OSError:
        return False


_current_process: subprocess.Popen[str] | None = None


def _terminate_daemon(process: subprocess.Popen[str] | None) -> None:
    """Terminate both the venv launcher and the daemon it spawned.

    On Windows, ``venv/Scripts/python.exe`` is a launcher whose child owns the
    HTTP listener, so ``process.pid`` is not the PID written to runtime.json.
    """

    data = read_runtime()
    daemon_pid = data.get("pid") if isinstance(data, dict) else None
    if (
        isinstance(daemon_pid, int)
        and daemon_pid != os.getpid()
        and (process is None or daemon_pid != process.pid)
    ):
        with contextlib.suppress(OSError):
            os.kill(daemon_pid, signal.SIGTERM)
    if process is None:
        return
    with contextlib.suppress(OSError):
        process.terminate()
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        process.wait(timeout=5)


def stop_daemon() -> None:
    global _current_process
    process = _current_process
    _current_process = None
    _terminate_daemon(process)


def boot_daemon_once(
    on_healthy: Callable[[], None] | None = None, *, timeout_s: float = 30.0
) -> BootResult:
    """Start `siwah serve`, wait for health, then wait for it to exit."""

    global _current_process
    with contextlib.suppress(OSError):
        runtime_path().unlink()
    env = dict(os.environ)
    env["SIWAH_SUPERVISED"] = "1"
    runtime_token = secrets.token_urlsafe(32)
    runtime_nonce = secrets.token_hex(16)
    env[RUNTIME_TOKEN_ENV] = runtime_token
    env[SUPERVISOR_NONCE_ENV] = runtime_nonce
    process: subprocess.Popen[str] | None = None
    log = None
    try:
        with contextlib.suppress(OSError):
            ensure_home()
            logs = siwah_home() / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            log = (logs / "daemon.log").open("a", encoding="utf-8")
        process = subprocess.Popen(
            [sys.executable, "-m", "siwah", "serve"],
            stdin=subprocess.DEVNULL,
            stdout=log or subprocess.DEVNULL,
            stderr=log or subprocess.DEVNULL,
            env=env,
            text=True,
        )
        _current_process = process
        deadline = time.monotonic() + timeout_s
        endpoint: RuntimeEndpoint | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return BootResult(
                    False,
                    process.returncode if process.returncode is not None else 1,
                    "daemon exited before becoming healthy",
                )
            # The venv launcher's PID differs from the daemon PID on Windows.
            # The one-boot token and HMAC nonce prove this is our child.
            endpoint = runtime_endpoint(token=runtime_token)
            if endpoint is not None and runtime_healthy(endpoint, nonce=runtime_nonce):
                break
            time.sleep(0.1)
        if endpoint is None or not runtime_healthy(endpoint, nonce=runtime_nonce):
            return BootResult(False, 1, "daemon did not become healthy")
        if on_healthy is not None:
            on_healthy()
        code = process.wait()
        return BootResult(True, code)
    finally:
        if process is not None:
            _terminate_daemon(process)
        _current_process = None
        if log is not None:
            log.close()


class Supervisor:
    def __init__(
        self,
        repo: Path | str,
        *,
        boot: Callable[[Callable[[], None]], BootResult],
        rollback: Callable[[Path], SnapshotResult] = rollback_to_tag,
        verify: Callable[[Path], ManifestStatus] = verify_manifest,
        snapshot: Callable[[Path, str], str | None] = create_healthy_snapshot,
        audit: Callable[..., dict[str, Any]] = audit_event,
        max_failures: int = 2,
    ) -> None:
        self.repo = Path(repo).resolve()
        self.boot = boot
        self.rollback = rollback
        self.verify = verify
        self.snapshot = snapshot
        self.audit = audit
        self.max_failures = max(1, max_failures)

    def run(self) -> int:
        if not self._recover_manifest():
            return 1
        failures = 0
        while True:
            try:
                result = self.boot(self._snapshot_healthy)
            except Exception as exc:  # supervisor must survive a failed launch
                result = BootResult(False, 1, f"{type(exc).__name__}: {exc}")
            if result.healthy:
                if result.exit_code == 0:
                    self.audit("boot.clean_exit", {"exit_code": 0})
                    return 0
                failures += 1
            else:
                failures += 1
            self.audit(
                "boot.failed",
                {
                    "failures": failures,
                    "healthy": result.healthy,
                    "exit_code": result.exit_code,
                    "error": result.error,
                },
            )
            if failures >= self.max_failures:
                rolled = self.rollback(self.repo)
                self.audit(
                    "rollback.auto",
                    {"ok": rolled.ok, "tag": rolled.tag, "detail": rolled.detail},
                )
                return 1

    def _recover_manifest(self) -> bool:
        status = self.verify(self.repo)
        if not status.present:
            if latest_healthy_tag(self.repo) is None:
                self.audit("manifest.missing", {})
                return True
            # The repository commits its manifest, so a missing manifest with a
            # healthy tag means the trust anchor was deleted after the fact.
            self.audit("manifest.missing", {"rollback": True})
        elif status.ok:
            dirty = protected_changes(self.repo)
            if not dirty:
                self.audit("manifest.ok", {"hash": status.actual_hash})
                return True
            if status.actual_hash and is_trusted_snapshot(self.repo, status.actual_hash):
                # The tree equals a verified healthy snapshot but differs from
                # HEAD because a rollback restored it; that is not tampering.
                self.audit("manifest.restored", {"hash": status.actual_hash})
                return True
            # A manifest that matches the working tree but not any healthy tag
            # is a re-minted anchor over edited protected files.
            self.audit("manifest.uncommitted", {"paths": dirty[:20]})
        else:
            self.audit(
                "manifest.mismatch",
                {
                    "expected": status.expected_hash,
                    "actual": status.actual_hash,
                    "changed": status.changed,
                    "missing": status.missing,
                    "extra": status.extra,
                },
            )
        rolled = self.rollback(self.repo)
        self.audit(
            "rollback.manifest",
            {"ok": rolled.ok, "tag": rolled.tag, "detail": rolled.detail},
        )
        if not rolled.ok:
            return False
        recovered = self.verify(self.repo)
        if not recovered.ok:
            self.audit("manifest.revert_failed", {"summary": recovered.summary()})
            return False
        self.audit("manifest.recovered", {"hash": recovered.actual_hash})
        return True

    def _snapshot_healthy(self) -> None:
        status = self.verify(self.repo)
        if not status.present or not status.ok or not status.actual_hash:
            return
        tag = self.snapshot(self.repo, status.actual_hash)
        if tag is not None:
            self.audit("snapshot.created", {"tag": tag, "hash": status.actual_hash})


__all__ = [
    "BootResult",
    "RUNTIME_TOKEN_ENV",
    "RuntimeEndpoint",
    "SUPERVISOR_NONCE_ENV",
    "Supervisor",
    "boot_daemon_once",
    "read_runtime",
    "runtime_endpoint",
    "runtime_healthy",
    "stop_daemon",
]
