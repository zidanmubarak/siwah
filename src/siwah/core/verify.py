"""Verifier-in-the-loop: project tests, lint, and type checks after edits."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..config import PermissionsConfig, VerifyConfig
from ..harness.memory import redact_secrets
from ..permissions.policy import PermissionPolicy, PermissionRequest
from ..process import terminate_process_tree
from ..sandbox import Sandbox

Status = Literal["passed", "failed", "unavailable", "flaky"]


class VerifyCommandResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    command: str
    ok: bool
    exit_code: int | None = None
    duration_s: float = 0.0
    timed_out: bool = False
    flaky: bool = False
    output: str = ""
    denied: bool = False
    execution_error: bool = False
    failure_fingerprint: str | None = None

    @property
    def incomplete(self) -> bool:
        return (
            self.denied
            or self.timed_out
            or self.execution_error
            or (not self.ok and self.exit_code in {None, 126, 127, 9009})
        )


class VerifyReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Status
    commands: list[VerifyCommandResult] = Field(default_factory=list)
    summary: str = ""

    def failing_names(self) -> set[str]:
        return {command.name for command in self.commands if not command.ok and not command.flaky}

    def new_failures(self, baseline: VerifyReport | None) -> list[VerifyCommandResult]:
        if baseline is None or baseline.status == "unavailable":
            return [command for command in self.commands if not command.ok and not command.flaky]
        previous = {
            self._failure_identity(command)
            for command in baseline.commands
            if not command.ok and not command.flaky and not command.incomplete
        }
        return [
            command
            for command in self.commands
            if not command.ok
            and not command.flaky
            and (command.incomplete or self._failure_identity(command) not in previous)
        ]

    @staticmethod
    def _failure_identity(command: VerifyCommandResult) -> tuple:
        # Compare the full-output digest when available, not just the command
        # name or a truncated tail. Changed/nondeterministic output is treated
        # conservatively as a potential regression.
        return (
            command.name,
            command.command,
            command.exit_code,
            command.timed_out,
            command.failure_fingerprint or command.output,
        )


@dataclass(frozen=True)
class VerifyCommand:
    name: str
    command: str


@dataclass
class _CommandRun:
    result: VerifyCommandResult
    raw: str = ""


class Verifier:
    """Run the workspace's verifier commands.

    `verify.toml` at the workspace root is authoritative. Otherwise configured
    commands are used, then a small project-type detector. Auto-detection only
    selects tools that are available in the current interpreter/PATH.
    """

    def __init__(
        self,
        workspace: Path,
        config: VerifyConfig,
        *,
        authorize: Callable[[PermissionRequest], Awaitable[tuple[bool, str | None]]] | None = None,
        sandbox: Sandbox | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.config = config
        self._commands: list[VerifyCommand] | None = None
        self._authorize = authorize
        self._policy = PermissionPolicy(self.workspace, PermissionsConfig())
        self.sandbox = sandbox

    async def _authorized_run(self, command: VerifyCommand) -> VerifyCommandResult:
        request = PermissionRequest(
            tool="bash",
            kind="bash",
            command=command.command,
            preview=f"Verifier {command.name}: {command.command}",
        )
        if self._authorize is None:
            allowed = self._policy.check(request) == "allow"
            reason = "Verifier shell permission required but no approver is available"
        else:
            allowed, reason = await self._authorize(request)
        if not allowed:
            return VerifyCommandResult(
                name=command.name,
                command=command.command,
                ok=False,
                denied=True,
                output=reason or "Verifier shell command denied",
            )
        return await self._run_command(command)

    def commands(self) -> list[VerifyCommand]:
        if self._commands is not None:
            return self._commands
        project = _load_verify_toml(self.workspace)
        if project:
            self._commands = project
            return self._commands
        if self.config.commands:
            self._commands = [
                VerifyCommand(name=name, command=command)
                for name, command in self.config.commands.items()
                if command.strip()
            ]
            return self._commands
        self._commands = _detect_commands(self.workspace)
        return self._commands

    async def run(self) -> VerifyReport:
        commands = self.commands()
        if not commands:
            return VerifyReport(
                status="unavailable",
                summary="Verifier: unavailable (no test, lint, or typecheck command detected)",
            )
        results: list[VerifyCommandResult] = []
        for command in commands:
            result = await self._authorized_run(command)
            if (
                not result.ok
                and command.name == "test"
                and self.config.retry_flaky
                and not result.incomplete
            ):
                retry = await self._authorized_run(command)
                if retry.ok:
                    retry.flaky = True
                    retry.output = (
                        "First run failed; the retry passed. The test is likely flaky.\n"
                        + retry.output
                    )
                    result = retry
            results.append(result)
        status: Status
        if any(not item.ok and not item.flaky for item in results):
            status = "failed"
        elif any(item.flaky for item in results):
            status = "flaky"
        else:
            status = "passed"
        return VerifyReport(status=status, commands=results, summary=_summary(results, status))

    async def _run_command(self, command: VerifyCommand) -> VerifyCommandResult:
        started = time.perf_counter()
        environment = _verify_environment()
        pycache = tempfile.mkdtemp(prefix="siwah-verify-")
        environment["PYTHONPYCACHEPREFIX"] = pycache
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        if self.sandbox is not None:
            try:
                result = await self.sandbox.shell(
                    command.command,
                    timeout_s=self.config.timeout_s,
                    env_extra={
                        "PYTHONPYCACHEPREFIX": pycache,
                        "PYTHONDONTWRITEBYTECODE": "1",
                    },
                )
            finally:
                shutil.rmtree(pycache, ignore_errors=True)
            return VerifyCommandResult(
                name=command.name,
                command=command.command,
                ok=result.ok,
                exit_code=result.returncode,
                duration_s=round(time.perf_counter() - started, 2),
                execution_error=not result.ok
                and (
                    result.backend.startswith("missing-")
                    or result.output.startswith(
                        (
                            "sandbox failed:",
                            "sandbox timed out after ",
                            "sandbox backend is unavailable:",
                        )
                    )
                ),
                output=redact_secrets(_trim_output(result.output, self.config.keep_output_chars)),
                failure_fingerprint=hashlib.sha256(result.output.encode()).hexdigest(),
            )
        try:
            process = await asyncio.create_subprocess_shell(
                command.command,
                cwd=str(self.workspace),
                env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            shutil.rmtree(pycache, ignore_errors=True)
            return VerifyCommandResult(
                name=command.name,
                command=command.command,
                ok=False,
                exit_code=None,
                duration_s=round(time.perf_counter() - started, 2),
                output=f"Cannot start command: {exc}",
            )
        timed_out = False
        reader = asyncio.create_task(process.communicate())
        try:
            stdout, _ = await asyncio.wait_for(
                asyncio.shield(reader), timeout=self.config.timeout_s
            )
        except TimeoutError:
            timed_out = True
            await terminate_process_tree(process)
            stdout, _ = await reader
        except asyncio.CancelledError:
            await terminate_process_tree(process)
            await reader
            raise
        finally:
            shutil.rmtree(pycache, ignore_errors=True)
        output = stdout.decode("utf-8", errors="replace")
        output = redact_secrets(_trim_output(output, self.config.keep_output_chars))
        return VerifyCommandResult(
            name=command.name,
            command=command.command,
            ok=process.returncode == 0 and not timed_out,
            exit_code=process.returncode,
            duration_s=round(time.perf_counter() - started, 2),
            timed_out=timed_out,
            output=output,
            failure_fingerprint=hashlib.sha256(stdout).hexdigest(),
        )


def _load_verify_toml(workspace: Path) -> list[VerifyCommand]:
    path = workspace / "verify.toml"
    if not path.is_file():
        return []
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    commands = payload.get("commands")
    if not isinstance(commands, dict):
        return []
    result: list[VerifyCommand] = []
    for name, command in commands.items():
        if isinstance(command, str) and command.strip():
            result.append(VerifyCommand(name=str(name), command=command))
    return result


def _detect_commands(workspace: Path) -> list[VerifyCommand]:
    commands: list[VerifyCommand] = []
    if _has_python(workspace):
        if importlib.util.find_spec("pytest") is not None:
            commands.append(VerifyCommand("test", f'"{sys.executable}" -m pytest -q'))
        if importlib.util.find_spec("ruff") is not None:
            commands.append(VerifyCommand("lint", f'"{sys.executable}" -m ruff check .'))
        pyright = shutil.which("pyright")
        if pyright:
            commands.append(VerifyCommand("typecheck", f'"{pyright}"'))
    package = workspace / "package.json"
    if package.is_file():
        try:
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
        except (OSError, json.JSONDecodeError):
            scripts = {}
        if isinstance(scripts, dict):
            runner = _js_runner(workspace)
            for name in ("test", "lint", "typecheck", "type-check"):
                if name in scripts:
                    commands.append(VerifyCommand(name, f"{runner} run {name}"))
    if (workspace / "go.mod").is_file() and shutil.which("go"):
        commands.extend(
            [
                VerifyCommand("test", "go test ./..."),
                VerifyCommand("lint", "go vet ./..."),
            ]
        )
    if (workspace / "Cargo.toml").is_file() and shutil.which("cargo"):
        commands.append(VerifyCommand("test", "cargo test"))
    return _dedupe(commands)


def _has_python(workspace: Path) -> bool:
    if (workspace / "pyproject.toml").is_file() or (workspace / "setup.py").is_file():
        return True
    if (workspace / "tests").is_dir():
        return True
    return any(workspace.glob("*.py"))


def _js_runner(workspace: Path) -> str:
    if shutil.which("bun") and (
        (workspace / "bun.lock").exists() or (workspace / "bun.lockb").exists()
    ):
        return "bun"
    return "npm"


def _dedupe(commands: list[VerifyCommand]) -> list[VerifyCommand]:
    seen: set[str] = set()
    result: list[VerifyCommand] = []
    for command in commands:
        if command.name in seen:
            continue
        seen.add(command.name)
        result.append(command)
    return result


def _verify_environment() -> dict[str, str]:
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if any(marker in upper for marker in ("API_KEY", "SECRET", "TOKEN", "PASSWORD")):
            continue
        env[key] = value
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _trim_output(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value.strip()
    return "... output truncated; showing the tail ...\n" + value[-limit:].strip()


def _summary(results: list[VerifyCommandResult], status: Status) -> str:
    lines = [f"Verifier: {status}"]
    for result in results:
        label = "PASS" if result.ok else ("TIMEOUT" if result.timed_out else "FAIL")
        if result.denied:
            label = "DENIED (not run)"
        if result.flaky:
            label = "FLAKY"
        lines.append(
            f"- {result.name}: {label} (exit {result.exit_code}, {result.duration_s:.2f}s)"
        )
    for result in results:
        if result.ok and not result.flaky:
            continue
        lines.append(f"\n[{result.name}] $ {result.command}")
        if result.output:
            lines.append(result.output)
    return "\n".join(lines)[:12_000]


__all__ = ["Verifier", "VerifyCommand", "VerifyCommandResult", "VerifyReport"]
