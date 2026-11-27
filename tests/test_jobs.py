from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from siwah.config import JobsConfig
from siwah.jobs import Job, JobManager


@pytest.mark.asyncio
async def test_background_job_list_tail_and_kill(tmp_path: Path):
    events: list[tuple[str, dict[str, Any]]] = []
    progress = asyncio.Event()

    async def emit(event: str, data: dict[str, Any]) -> None:
        events.append((event, data))
        if event == "job.progress":
            progress.set()

    manager = JobManager(JobsConfig(max_jobs=2, tail_lines=20), cwd=tmp_path)
    try:
        job = await manager.start(
            "python -u -c \"print('hello'); import time; time.sleep(5)\"",
            tool_call_id="shell-call",
            emit=emit,
        )
        await asyncio.wait_for(progress.wait(), timeout=3)
        for _ in range(30):
            if "hello" in manager.tail(job.id):
                break
            await asyncio.sleep(0.1)
        assert job.id in [item.id for item in manager.list()]
        assert "hello" in manager.tail(job.id)
        assert await manager.kill(job.id) is True
        assert job.status == "killed"
    finally:
        await manager.close()
    assert [name for name, _ in events] == ["job.started", "job.progress", "job.completed"]
    assert all(data["tool_call_id"] == "shell-call" for _, data in events)
    assert events[1][1]["lines_read"] == 1
    assert events[-1][1]["status"] == "killed"


@pytest.mark.asyncio
async def test_kill_does_not_override_a_successful_exit(tmp_path: Path):
    manager = JobManager(JobsConfig(max_jobs=1), cwd=tmp_path)
    job = Job(
        id="finished",
        command="true",
        process=type("FinishedProcess", (), {"returncode": 0})(),  # type: ignore[arg-type]
        started_at="now",
    )
    manager.jobs[job.id] = job
    assert await manager.kill(job.id) is False
    assert job.status == "running"


@pytest.mark.asyncio
async def test_job_limit_is_enforced(tmp_path: Path):
    manager = JobManager(JobsConfig(max_jobs=1), cwd=tmp_path)
    try:
        await manager.start('python -c "import time; time.sleep(5)"')
        with pytest.raises(RuntimeError, match="maximum background jobs"):
            await manager.start('python -c "import time; time.sleep(5)"')
    finally:
        await manager.close()
