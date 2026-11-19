from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from siwah.config import JobsConfig
from siwah.jobs import JobManager


@pytest.mark.asyncio
async def test_background_job_list_tail_and_kill(tmp_path: Path):
    manager = JobManager(JobsConfig(max_jobs=2, tail_lines=20), cwd=tmp_path)
    try:
        job = await manager.start("python -u -c \"print('hello'); import time; time.sleep(5)\"")
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


@pytest.mark.asyncio
async def test_job_limit_is_enforced(tmp_path: Path):
    manager = JobManager(JobsConfig(max_jobs=1), cwd=tmp_path)
    try:
        await manager.start('python -c "import time; time.sleep(5)"')
        with pytest.raises(RuntimeError, match="maximum background jobs"):
            await manager.start('python -c "import time; time.sleep(5)"')
    finally:
        await manager.close()
