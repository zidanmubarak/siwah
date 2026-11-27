from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from siwah.harness.bootstrap import ensure_harness
from siwah.harness.skill_usage import (
    archived_names,
    curate,
    load_usage,
    maybe_curate,
    record_event,
    set_pinned,
    state_for,
)
from siwah.harness.skills import create_skill, list_skills


def test_skill_usage_tracks_views_patches_and_archives_stale_agent_skills(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    ensure_harness()
    create_skill("verify", "Verify a change.", "Run the tests.")
    record_event("verify", "create", created_by="agent")
    record_event("verify", "view")
    record_event("verify", "patch")

    record = load_usage()["verify"]
    assert record["created_by"] == "agent"
    assert record["view_count"] == 1
    assert record["patch_count"] == 1
    assert state_for("verify") == "active"

    later = datetime.now(UTC) + timedelta(days=40)
    result = curate(now=later)
    assert result["archived"] == 1
    assert state_for("verify") == "archived"
    assert "verify" in archived_names()
    assert all(skill.name != "verify" for skill in list_skills())

    set_pinned("verify", True)
    assert load_usage()["verify"]["pinned"] is True


def test_curator_scheduler_runs_once_per_interval(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SIWAH_CURATOR", "1")
    ensure_harness()
    create_skill("scheduled", "Scheduled skill.", "Do the thing.")
    record_event("scheduled", "create", created_by="agent")

    later = datetime.now(UTC) + timedelta(days=40)
    first = maybe_curate(now=later)
    assert first["status"] == "ran"
    assert state_for("scheduled") == "archived"
    second = maybe_curate(now=later + timedelta(hours=1))
    assert second["status"] == "not_due"
