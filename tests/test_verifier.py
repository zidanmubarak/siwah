from __future__ import annotations

import sys
from pathlib import Path

import pytest

from siwah.config import AgentConfig, PermissionsConfig, VerifyConfig
from siwah.core.storage import Store
from siwah.permissions.policy import PermissionPolicy
from siwah.tools.base import ToolContext

from .conftest import ScriptedAdapter, make_loop, outcome, tool_call


def _repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    (repo / "math_utils.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "check.py").write_text(
        "import sys\nfrom math_utils import add\nsys.exit(0 if add(2, 3) == 5 else 1)\n",
        encoding="utf-8",
    )
    python = Path(sys.executable).as_posix()
    (repo / "verify.toml").write_text(
        f"[commands]\ntest = '\"{python}\" check.py'\n", encoding="utf-8"
    )
    return repo


@pytest.mark.asyncio
async def test_verifier_loop_repairs_synthetic_failures(config, store: Store, tmp_path: Path):
    cfg = config.model_copy(
        update={
            "verify": VerifyConfig(auto=True, retry_flaky=False, max_repair_steps=2),
            "agent": config.agent.model_copy(update={"max_steps": 8}),
        }
    )

    async def approve(_request, _request_id):
        from siwah.permissions.policy import Approval

        return Approval(decision="once")

    async def emit(_event_type: str, _data: dict) -> None:
        return None

    passed = 0
    for index in range(5):
        repo = _repo(tmp_path, f"case-{index}")
        adapter = ScriptedAdapter(
            [
                outcome(
                    tool_calls=[
                        tool_call(
                            "edit_file",
                            {
                                "path": "math_utils.py",
                                "old_string": "return a + b",
                                "new_string": "return a - b",
                            },
                            f"break_{index}",
                        )
                    ]
                ),
                outcome(text="Done."),
                outcome(
                    tool_calls=[
                        tool_call(
                            "edit_file",
                            {
                                "path": "math_utils.py",
                                "old_string": "return a - b",
                                "new_string": "return a + b",
                            },
                            f"fix_{index}",
                        )
                    ]
                ),
                outcome(text="Fixed."),
            ],
            cfg.providers["mock"],
        )
        loop = make_loop(cfg, store, adapter)
        session = store.create_session(cwd=str(repo), model="mock/model-a")
        result = await loop.run(
            session,
            "Keep the verifier green",
            emit=emit,
            permission_resolver=approve,
        )
        if result.error is None and (repo / "math_utils.py").read_text().endswith("return a + b\n"):
            passed += 1

    assert passed >= 4, f"verifier repaired {passed}/5 synthetic cases"


@pytest.mark.asyncio
async def test_verifier_marks_a_flaky_failure(tmp_path: Path):
    from siwah.core.verify import Verifier

    repo = tmp_path / "flaky"
    repo.mkdir()
    python = Path(sys.executable).as_posix()
    (repo / "check.py").write_text(
        "from pathlib import Path\n"
        "counter = Path('count.txt')\n"
        "value = int(counter.read_text()) if counter.exists() else 0\n"
        "counter.write_text(str(value + 1))\n"
        "raise SystemExit(0 if value >= 1 else 1)\n",
        encoding="utf-8",
    )
    (repo / "verify.toml").write_text(
        f"[commands]\ntest = '\"{python}\" check.py'\n", encoding="utf-8"
    )
    context = ToolContext(
        workspace=repo,
        policy=PermissionPolicy(repo, PermissionsConfig(mode="yolo")),
        session_id="verifier-fixture",
        agent_config=AgentConfig(),
    )
    verifier = Verifier(
        repo, VerifyConfig(auto=True, retry_flaky=True), authorize=context.authorize
    )
    report = await verifier.run()
    assert report.status == "flaky"
    assert report.commands[0].flaky is True


@pytest.mark.asyncio
async def test_verifier_unavailable_does_not_block(tmp_path: Path):
    from siwah.core.verify import Verifier

    repo = tmp_path / "empty"
    repo.mkdir()
    report = await Verifier(repo, VerifyConfig(auto=True)).run()
    assert report.status == "unavailable"
    assert report.commands == []


@pytest.mark.asyncio
async def test_verifier_blocks_an_unexplained_completion(config, store: Store, tmp_path: Path):
    cfg = config.model_copy(
        update={
            "verify": VerifyConfig(auto=True, retry_flaky=False, max_repair_steps=1),
            "agent": config.agent.model_copy(update={"max_steps": 6}),
        }
    )
    repo = _repo(tmp_path, "blocked")
    adapter = ScriptedAdapter(
        [
            outcome(
                tool_calls=[
                    tool_call(
                        "edit_file",
                        {
                            "path": "math_utils.py",
                            "old_string": "return a + b",
                            "new_string": "return a - b",
                        },
                        "break",
                    )
                ]
            ),
            outcome(text="Done."),
            outcome(text="Done."),
        ],
        cfg.providers["mock"],
    )
    loop = make_loop(cfg, store, adapter)
    session = store.create_session(cwd=str(repo), model="mock/model-a")

    async def emit(_event_type: str, _data: dict) -> None:
        return None

    result = await loop.run(session, "Fix it", emit=emit)
    assert result.error == "verification_failed"
    updated = store.get_session(session.id)
    assert updated is not None
    assert updated.status == "error"
