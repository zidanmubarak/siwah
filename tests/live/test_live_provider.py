"""Live smoke test against the configured provider.

Run only with explicit opt-in and a budget-friendly model:

    SIWAH_LIVE=1 SIWAH_LIVE_MODEL=provider/model-id \
    SIWAH_CONFIG=/path/to/config.toml \
    uv run pytest tests/live -q
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from siwah.auth import AuthStore
from siwah.config import load_config
from siwah.core.loop import AgentLoop
from siwah.core.storage import Store
from siwah.providers.registry import ProviderRegistry, RoleRouter
from siwah.tools.base import ToolRegistry
from siwah.tools.runtime import default_tools


@pytest.mark.skipif(
    os.environ.get("SIWAH_LIVE") != "1" or not os.environ.get("SIWAH_LIVE_MODEL"),
    reason="set SIWAH_LIVE=1 and SIWAH_LIVE_MODEL=provider/model-id",
)
@pytest.mark.asyncio
async def test_live_read_only_smoke(tmp_path: Path):
    config = load_config()
    model = os.environ["SIWAH_LIVE_MODEL"]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("# live smoke\n", encoding="utf-8")
    store = Store(tmp_path / "state.db")
    registry = ProviderRegistry(config, AuthStore())
    router = RoleRouter(config, registry)
    loop = AgentLoop(
        config=config,
        store=store,
        router=router,
        tools=ToolRegistry(default_tools()),
    )
    session = store.create_session(cwd=str(workspace), model=model)
    events: list[str] = []

    async def emit(event_type: str, _data: dict) -> None:
        events.append(event_type)

    try:
        result = await loop.run(
            session,
            "Read README.md and reply with its first line. Do not edit any file.",
            emit=emit,
            model_override=model,
            max_steps=4,
        )
    finally:
        await registry.aclose()
        store.close()
    assert result.error is None
    assert "live smoke" in result.text.lower()
    assert result.usage.allowance >= 0
