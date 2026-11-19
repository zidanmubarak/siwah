"""Execute candidate code outside the grading interpreter, not an OS sandbox."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..config import Config
from .eval_models import EvalError, EvalTask
from .process import terminate_process_tree


class SubjectObservation(BaseModel):
    """Worker observations deliberately have no pass/fail or check-result field."""

    model_config = ConfigDict(extra="forbid")

    text: str = ""
    steps: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    allowance: float = Field(default=0, ge=0, allow_inf_nan=False)
    tool_calls: int = Field(default=0, ge=0)
    tool_errors: int = Field(default=0, ge=0)
    error: str | None = None


async def execute_subject(
    *,
    workspace: Path,
    state: Path,
    task: EvalTask,
    config: Config,
    auth_path: Path,
    harness: str,
    model: str,
    live: bool,
    max_steps: int,
    timeout_s: float,
) -> SubjectObservation:
    trusted_source = Path(__file__).resolve().parents[2]
    subject_source = Path(os.environ.get("SIWAH_SUBJECT_SOURCE", str(trusted_source))).resolve()
    bootstrap = (
        "import sys; "
        f"sys.path.insert(0, {str(subject_source)!r}); "
        "from siwah.kernel.subject_worker import main; main()"
    )
    payload = {
        "workspace": str(workspace),
        "state": str(state),
        "task": task.model_copy(
            update={"check": None, "check_text": None, "trap": None, "protected": []}
        ).model_dump(mode="json"),
        "config": config.model_dump(mode="json"),
        "auth_path": str(auth_path),
        "harness": harness,
        "model": model,
        "live": live,
        "max_steps": max_steps,
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-B",
        "-c",
        bootstrap,
        cwd=workspace,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    reader = asyncio.create_task(process.communicate(json.dumps(payload).encode("utf-8")))
    try:
        stdout, _stderr = await asyncio.wait_for(asyncio.shield(reader), timeout=timeout_s)
    except (asyncio.CancelledError, TimeoutError):
        await terminate_process_tree(process)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(reader, timeout=5)
        if not reader.done():
            reader.cancel()
        raise
    if process.returncode != 0:
        # Arbitrary worker stderr can contain credentials; do not replay it.
        raise EvalError(f"candidate worker exited with code {process.returncode}")
    try:
        return SubjectObservation.model_validate_json(stdout)
    except ValueError as exc:
        raise EvalError("candidate worker returned invalid observations") from exc


__all__ = ["SubjectObservation", "execute_subject"]
