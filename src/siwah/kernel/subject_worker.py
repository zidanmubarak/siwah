"""Candidate execution only; no task checking or score authority in this process."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path


async def run(payload: dict) -> dict:
    # These are subject-under-test imports, not dependencies of the judge.
    # They execute only in the disposable child interpreter.
    from siwah.auth import AuthStore
    from siwah.config import Config
    from siwah.core.loop import AgentLoop
    from siwah.core.storage import Store
    from siwah.eval.mock import MOCK_PROVIDER, MockEvalAdapter, basic_tools
    from siwah.kernel.eval_models import EvalTask
    from siwah.permissions.policy import Approval, is_risky_command
    from siwah.providers.registry import ProviderRegistry, RoleRouter
    from siwah.tools.base import ToolRegistry
    from siwah.tools.runtime import default_tools

    config = Config.model_validate(payload["config"])
    task = EvalTask.model_validate(payload["task"])
    store = Store(Path(payload["state"]))
    registry = ProviderRegistry(config, AuthStore(Path(payload["auth_path"])))
    if not payload["live"]:
        registry._adapters[MOCK_PROVIDER] = MockEvalAdapter(task, config.providers[MOCK_PROVIDER])
    loop = AgentLoop(
        config=config,
        store=store,
        router=RoleRouter(config, registry),
        tools=ToolRegistry(default_tools() if payload["harness"] == "siwah" else basic_tools()),
    )
    session = store.create_session(cwd=payload["workspace"], model=payload["model"])

    async def emit(_event_type: str, _data: dict) -> None:
        return None

    async def approve(request, _request_id: str) -> Approval:
        if request.kind == "bash" and is_risky_command(request.command or ""):
            return Approval(decision="deny", reason="eval guard")
        return Approval(decision="once")

    try:
        result = await loop.run(
            session,
            task.prompt,
            emit=emit,
            permission_resolver=approve,
            model_override=payload["model"],
            max_steps=payload["max_steps"],
        )
        usage = store.usage_totals(session.id)
        calls = store.get_tool_calls(session.id)
        return {
            "text": result.text,
            "steps": result.steps,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "allowance": usage.allowance,
            "tool_calls": len(calls),
            "tool_errors": sum(1 for call in calls if call["status"] != "ok"),
            "error": result.error or ("aborted" if result.aborted else None),
        }
    finally:
        await loop.close_jobs()
        await registry.aclose()
        store.close()


def main() -> None:
    payload = json.loads(sys.stdin.buffer.read())
    with contextlib.redirect_stdout(sys.stderr):
        observed = asyncio.run(run(payload))
    sys.stdout.write(json.dumps(observed))


if __name__ == "__main__":
    main()
