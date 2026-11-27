"""Background shell jobs with bounded output and lifecycle controls."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import JobsConfig
from .kernel.secrets import redact_secrets
from .process import terminate_process_tree
from .shell import shell_argv

JobEmitter = Callable[[str, dict[str, Any]], Awaitable[None]]
JobSink = Callable[["Job"], Awaitable[None]]


@dataclass
class Job:
    id: str
    command: str
    process: asyncio.subprocess.Process
    started_at: str
    status: str = "running"
    output: deque[str] = field(default_factory=deque)
    returncode: int | None = None
    tool_call_id: str | None = None
    lines_read: int = 0
    kill_requested: bool = False

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "command": self.command,
            "status": self.status,
            "returncode": self.returncode,
            "started_at": self.started_at,
            "tool_call_id": self.tool_call_id,
            "lines_read": self.lines_read,
        }


class JobManager:
    def __init__(
        self,
        config: JobsConfig,
        *,
        cwd: Path | str,
        emit: JobEmitter | None = None,
        environment: dict[str, str] | None = None,
        on_complete: JobSink | None = None,
    ) -> None:
        self.config = config
        self.cwd = Path(cwd)
        self.emit = emit
        self.environment = environment
        self.on_complete = on_complete
        self.jobs: dict[str, Job] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    async def start(
        self, command: str, *, tool_call_id: str | None = None, emit: JobEmitter | None = None
    ) -> Job:
        if (
            len([job for job in self.jobs.values() if job.status == "running"])
            >= self.config.max_jobs
        ):
            raise RuntimeError(f"maximum background jobs ({self.config.max_jobs}) reached")
        process = await asyncio.create_subprocess_exec(
            *shell_argv(command),
            cwd=str(self.cwd),
            env=self.environment or safe_environment(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        job = Job(
            id=f"job_{uuid.uuid4().hex[:12]}",
            command=command,
            process=process,
            started_at=datetime.now(UTC).isoformat(),
            tool_call_id=tool_call_id,
            output=deque(maxlen=max(20, self.config.tail_lines)),
        )
        self.jobs[job.id] = job
        sink = emit or self.emit
        task = asyncio.create_task(self._reader(job, sink))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    @staticmethod
    async def _publish(sink: JobEmitter | None, event: str, job: Job) -> None:
        if sink is not None:
            with contextlib.suppress(Exception):
                await sink(event, {**job.summary(), "command": redact_secrets(job.command)})

    async def _reader(self, job: Job, sink: JobEmitter | None) -> None:
        assert job.process.stdout is not None
        stream = job.process.stdout
        buffer = b""
        last_progress = 0.0
        try:
            await self._publish(sink, "job.started", job)
            while True:
                # read() has no line-length limit; readline() raises on a single
                # line larger than 64 KiB and would leave the job "running".
                chunk = await stream.read(65536)
                if not chunk:
                    break
                lines = (buffer + chunk).split(b"\n")
                buffer = lines.pop()
                for line in lines:
                    job.output.append(line.decode("utf-8", errors="replace") + "\n")
                job.lines_read += len(lines)
                if lines and time.monotonic() - last_progress >= 0.25:
                    last_progress = time.monotonic()
                    await self._publish(sink, "job.progress", job)
            if buffer:
                job.output.append(buffer.decode("utf-8", errors="replace"))
                job.lines_read += 1
            job.returncode = await job.process.wait()
            if job.status == "running":
                job.status = (
                    "completed"
                    if job.returncode == 0
                    else "killed"
                    if job.kill_requested
                    else "failed"
                )
        finally:
            if job.status == "running":
                job.status = "failed"
            await self._publish(sink, "job.completed", job)
            if self.on_complete is not None:
                with contextlib.suppress(Exception):
                    await self.on_complete(job)

    def list(self) -> list[Job]:
        return list(self.jobs.values())

    def tail(self, job_id: str, lines: int | None = None) -> str:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        limit = min(max(lines or self.config.tail_lines, 1), self.config.output_limit)
        return "".join(list(job.output)[-limit:])

    async def kill(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.status != "running":
            return False
        # The process may have exited successfully while its reader is still
        # draining the final bytes. Do not turn that race into a false "killed".
        if job.process.returncode == 0:
            return False
        job.kill_requested = True
        try:
            await terminate_process_tree(job.process)
        except (OSError, asyncio.CancelledError):
            job.kill_requested = False
            raise
        job.returncode = job.process.returncode
        job.status = "completed" if job.returncode == 0 else "killed"
        return job.status == "killed"

    async def close(self) -> None:
        for job in list(self.jobs.values()):
            if job.status == "running":
                with contextlib.suppress(ProcessLookupError, OSError):
                    await self.kill(job.id)
        # Let readers consume EOF and reap their subprocesses before cancelling
        # a stuck stream/emitter. Cancelling immediately can leak pipe transports.
        tasks = list(self._tasks)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=2.0)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def safe_environment() -> dict[str, str]:
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if any(marker in upper for marker in ("API_KEY", "SECRET", "TOKEN", "PASSWORD")):
            continue
        env[key] = value
    return env


async def stop_and_drain(
    process: asyncio.subprocess.Process,
    reader: asyncio.Task[tuple[bytes, bytes | None]],
    *,
    terminate: Callable[[asyncio.subprocess.Process], Awaitable[None]],
    cleanup: Callable[[], Awaitable[None]] | None = None,
) -> bytes:
    """Finish termination and join the output reader even on repeated cancellation."""

    async def finish() -> bytes:
        stdout = b""
        try:
            await terminate(process)
        finally:
            if os.name != "nt":
                # The group can outlive a shell that exited on SIGTERM while
                # one of its children ignored it.
                with contextlib.suppress(OSError):
                    os.killpg(process.pid, signal.SIGKILL)
            try:
                if cleanup is not None:
                    await cleanup()
            finally:
                try:
                    stdout, _ = await asyncio.wait_for(asyncio.shield(reader), timeout=2.0)
                except TimeoutError:
                    # An ignoring descendant can retain the pipe after its
                    # parent exits. All callers start a new process session.
                    if os.name != "nt":
                        with contextlib.suppress(OSError):
                            os.killpg(process.pid, signal.SIGKILL)
                    elif process.returncode is None:
                        with contextlib.suppress(ProcessLookupError):
                            process.kill()
                finally:
                    if not reader.done():
                        reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)
        return stdout

    task = asyncio.create_task(finish())
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
    if interrupted:
        # Retrieve cleanup errors, but cancellation remains the caller's result.
        task.exception()
        raise asyncio.CancelledError
    return task.result()


__all__ = ["Job", "JobManager", "safe_environment", "stop_and_drain"]
