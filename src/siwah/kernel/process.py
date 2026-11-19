"""Kernel-owned process-tree termination for shells and background jobs."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess


async def terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            with contextlib.suppress(ProcessLookupError):
                process.kill()
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            with contextlib.suppress(ProcessLookupError):
                process.kill()
    with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
        await asyncio.wait_for(process.wait(), timeout=5)


__all__ = ["terminate_process_tree"]
