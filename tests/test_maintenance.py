from __future__ import annotations

from pathlib import Path

import pytest

from siwah.core.storage import Store
from siwah.maintenance import inspect_home, prune_orphans, rotate_daemon_log
from siwah.paths import ensure_home, siwah_home


def test_inspect_and_prune_orphaned_session_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    ensure_home()
    store = Store(tmp_path / "state.db")
    try:
        live = store.create_session(cwd=str(tmp_path), model="mock/model-a")
        home = siwah_home()
        for name in ("ses_orphan", live.id):
            (home / "checkpoints" / name).mkdir(parents=True)
            (home / "trajectories" / f"{name}.jsonl").write_text("{}\n", encoding="utf-8")
        (home / "tool_outputs" / "ses_orphan").mkdir(parents=True)
        (home / "tool_outputs" / "ses_context").mkdir(parents=True)
        (home / "logs").mkdir(parents=True)
        (home / "logs" / "daemon.log").write_text("x" * 100, encoding="utf-8")

        report = inspect_home(store)
        assert report.checkpoint_orphans == ["ses_orphan"]
        assert report.trajectory_orphans == ["ses_orphan"]
        assert report.tool_output_orphans == ["ses_orphan"]
        assert report.log_bytes == 100

        removed = prune_orphans(store)
        assert len(removed) == 3
        assert not (home / "checkpoints" / "ses_orphan").exists()
        assert not (home / "trajectories" / "ses_orphan.jsonl").exists()
        assert not (home / "tool_outputs" / "ses_orphan").exists()
        assert (home / "checkpoints" / live.id).exists()
        assert (home / "trajectories" / f"{live.id}.jsonl").exists()
        assert (home / "tool_outputs" / "ses_context").exists()

        (home / "checkpoints" / "ses_dry").mkdir()
        dry = prune_orphans(store, dry_run=True)
        assert [path.name for path in dry] == ["ses_dry"]
        assert (home / "checkpoints" / "ses_dry").exists()
    finally:
        store.close()


def test_rotate_daemon_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import siwah.maintenance as maintenance

    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    ensure_home()
    (siwah_home() / "logs").mkdir(parents=True, exist_ok=True)
    log = siwah_home() / "logs" / "daemon.log"
    log.write_text("large enough", encoding="utf-8")
    monkeypatch.setattr(maintenance, "LOG_ROTATE_BYTES", 1)
    rotated = rotate_daemon_log()
    assert rotated is not None
    assert rotated == log.with_suffix(".log.1")
    assert rotated.exists()
    assert not log.exists()
