"""Opt-in command sandbox using Docker or bubblewrap with a local fallback."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .config import SandboxConfig
from .jobs import safe_environment, stop_and_drain
from .process import terminate_process_tree
from .shell import shell_argv


@dataclass(frozen=True)
class SandboxResult:
    ok: bool
    output: str
    returncode: int
    backend: str


class SandboxError(RuntimeError):
    pass


class Sandbox:
    def __init__(self, config: SandboxConfig, *, cwd: Path | str) -> None:
        self.config = config
        self.cwd = Path(cwd).resolve()

    def backend(self) -> str:
        if self.config.backend == "local":
            return "local"
        if self.config.backend == "docker":
            return "docker" if shutil.which("docker") else "missing-docker"
        if self.config.backend == "bwrap":
            return "bwrap" if shutil.which("bwrap") else "missing-bwrap"
        if shutil.which("docker") and self.config.image:
            return "docker"
        if sys.platform.startswith("linux") and shutil.which("bwrap"):
            return "bwrap"
        return "local"

    async def run(
        self,
        command: list[str],
        *,
        timeout_s: float = 120.0,
        env_extra: dict[str, str] | None = None,
    ) -> SandboxResult:
        backend = self.backend()
        if backend.startswith("missing-"):
            return SandboxResult(False, f"sandbox backend is unavailable: {backend}", 1, backend)
        container: str | None = None
        if backend == "docker":
            if not self.config.image:
                return SandboxResult(
                    False, "sandbox backend docker requires [sandbox].image", 1, backend
                )
            container = f"siwah-sandbox-{os.getpid()}-{uuid4().hex[:8]}"
            argv = [
                "docker",
                "run",
                "--rm",
                "--name",
                container,
                "--network",
                "bridge" if self.config.network else "none",
                "-v",
                f"{self.cwd}:/workspace",
                "-w",
                "/workspace",
                self.config.image,
                *command,
            ]
        elif backend == "bwrap":
            argv = [
                "bwrap",
                "--die-with-parent",
                "--ro-bind",
                "/",
                "/",
                "--bind",
                str(self.cwd),
                str(self.cwd),
                "--chdir",
                str(self.cwd),
                *([] if self.config.network else ["--unshare-net"]),
                "--",
                *command,
            ]
        else:
            argv = command
        env = safe_environment() if backend == "local" else None
        if env is not None and env_extra:
            env.update(env_extra)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(self.cwd),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            return SandboxResult(False, f"sandbox failed: {exc}", 1, backend)
        reader = asyncio.create_task(process.communicate())

        async def cleanup() -> None:
            if container is not None:
                await self._remove_container(container)

        try:
            stdout, _ = await asyncio.wait_for(asyncio.shield(reader), timeout=timeout_s)
        except asyncio.CancelledError:
            await stop_and_drain(process, reader, terminate=terminate_process_tree, cleanup=cleanup)
            raise
        except TimeoutError:
            await stop_and_drain(process, reader, terminate=terminate_process_tree, cleanup=cleanup)
            return SandboxResult(False, f"sandbox timed out after {timeout_s:g}s", 1, backend)
        output = stdout.decode("utf-8", errors="replace")
        returncode = process.returncode if process.returncode is not None else 1
        if returncode != 0:
            return SandboxResult(False, output, returncode, backend)
        return SandboxResult(True, output or "(no output)", 0, backend)

    async def shell(
        self,
        command: str,
        *,
        timeout_s: float = 120.0,
        env_extra: dict[str, str] | None = None,
    ) -> SandboxResult:
        backend = self.backend()
        argv = ["sh", "-lc", command] if backend in {"docker", "bwrap"} else shell_argv(command)
        return await self.run(argv, timeout_s=timeout_s, env_extra=env_extra)

    @staticmethod
    async def _remove_container(name: str) -> None:
        try:
            cleanup = await asyncio.create_subprocess_exec(
                "docker",
                "rm",
                "-f",
                name,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(cleanup.wait(), timeout=10)
        except TimeoutError:
            await terminate_process_tree(cleanup)
        except OSError:
            pass


__all__ = ["Sandbox", "SandboxError", "SandboxResult"]
