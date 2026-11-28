from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from siwah.cli.main import app
from siwah.config import Config, CronConfig
from siwah.core.cron import (
    CronBusy,
    CronError,
    CronJob,
    CronRun,
    CronScheduler,
    CronStore,
    execute_job,
    next_run,
    parse_schedule,
    run_lock,
    validate_job,
)
from siwah.core.storage import Store, UsageTotals
from siwah.permissions.policy import PermissionRequest


def _now() -> datetime:
    return datetime.now().astimezone()


def _job(tmp_path: Path, **overrides) -> CronJob:
    values = {
        "id": "nightly",
        "schedule": "0 9 * * *",
        "cwd": str(tmp_path),
        "prompt": "Run the nightly checks.",
        "max_allowance": 0.25,
        "timeout_s": 5,
        "next_run_at": (_now() - timedelta(minutes=1)).isoformat(),
    }
    values.update(overrides)
    return CronJob(**values)


def test_cron_parses_fields_and_finds_the_next_minute():
    schedule = parse_schedule("*/15 9-10 * * 1-5")
    assert sorted(schedule.minutes) == [0, 15, 30, 45]
    assert sorted(schedule.hours) == [9, 10]
    assert sorted(schedule.weekdays) == [1, 2, 3, 4, 5]
    after = datetime(2026, 11, 27, 9, 7, tzinfo=UTC)  # Friday
    assert next_run(schedule, after) == datetime(2026, 11, 27, 9, 15, tzinfo=UTC)


def test_cron_day_of_month_and_weekday_use_or_semantics():
    schedule = parse_schedule("0 9 1 * 1")
    after = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)  # Tuesday
    upcoming = next_run(schedule, after)
    assert upcoming is not None
    # Monday the 9th or the 1st of December, whichever comes first.
    assert upcoming.day in {9} or upcoming.day == 1


def test_cron_sunday_accepts_zero_and_seven():
    assert parse_schedule("0 9 * * 0").weekdays == parse_schedule("0 9 * * 7").weekdays
    assert next_run(parse_schedule("0 9 * * 7"), datetime(2026, 11, 27, 10, tzinfo=UTC)) == (
        datetime(2026, 11, 29, 9, tzinfo=UTC)
    )


@pytest.mark.parametrize(
    "value",
    [
        "* * * *",
        "60 * * * *",
        "* 24 * * *",
        "* * 0 * *",
        "* * * 13 *",
        "* * * * 8",
        "*/0 * * * *",
        "1-2-3 * * * *",
        "x * * * *",
    ],
)
def test_cron_rejects_invalid_schedules(value: str):
    with pytest.raises(CronError):
        parse_schedule(value)


def test_cron_store_roundtrip_and_duplicate_rejection(tmp_path: Path):
    store = CronStore(tmp_path)
    job = _job(tmp_path)
    store.add(job)
    loaded = store.get("nightly")
    assert loaded is not None
    assert loaded.schedule == "0 9 * * *"
    assert loaded.cwd == str(tmp_path)
    with pytest.raises(CronError, match="already exists"):
        store.add(job)
    assert store.set_enabled("nightly", False) is not None
    assert store.get("nightly").enabled is False  # type: ignore[union-attr]
    assert store.remove("nightly") is True
    assert store.get("nightly") is None


def test_cron_rejects_non_finite_limits(tmp_path: Path):
    job = _job(tmp_path)
    for field in ("max_allowance", "timeout_s"):
        with pytest.raises(CronError, match="finite"):
            validate_job(job.model_copy(update={field: float("nan")}))


def test_cron_store_tracks_daily_usage(tmp_path: Path):
    store = CronStore(tmp_path)
    job = _job(tmp_path)
    store.add(job)
    updated = store.update_job(
        job.id, lambda item: setattr(item, "last_status", "ok"), allowance=0.12
    )
    assert updated is not None
    assert store.used_today() == pytest.approx(0.12)
    assert (
        json.loads((tmp_path / "cron.json").read_text(encoding="utf-8"))["jobs"][0]["last_status"]
        == "ok"
    )


@pytest.mark.asyncio
async def test_execute_job_creates_a_workspace_session(config, store: Store, sample_repo: Path):
    from .conftest import ScriptedAdapter, make_loop, outcome

    adapter = ScriptedAdapter([outcome(text="checks complete")], config.providers["mock"])
    loop = make_loop(config, store, adapter)
    job = _job(
        sample_repo,
        prompt="Check the test suite.",
        next_run_at=None,
        max_allowance=0.1,
        timeout_s=5,
    )
    record = await execute_job(
        job,
        config=config,
        store=store,
        router=loop.router,
        agent=loop,
    )
    assert record.status == "ok"
    assert record.session_id is not None
    session = store.get_session(record.session_id)
    assert session is not None
    assert session.cwd == str(sample_repo.resolve())
    assert session.metadata["cron_job"] == "nightly"
    assert session.status == "idle"


@pytest.mark.asyncio
async def test_execute_job_permission_allow_list(config, store: Store, sample_repo: Path):
    seen: list[bool] = []

    class FakeAgent:
        async def run(self, session, prompt, *, permission_resolver, **_kwargs):
            approval = await permission_resolver(
                PermissionRequest(tool="bash", kind="bash", preview="pytest"), "req"
            )
            seen.append(approval.decision != "deny")
            return type(
                "Result",
                (),
                {
                    "steps": 1,
                    "usage": UsageTotals(allowance=0.01),
                    "aborted": False,
                    "error": None,
                },
            )()

    job = _job(sample_repo, prompt="Run tests.", next_run_at=None)
    await execute_job(
        job,
        config=config,
        store=store,
        router=cast(Any, object()),
        agent=cast(Any, FakeAgent()),
    )
    assert seen == [False]

    allowed_job = job.model_copy(update={"allow": ["bash"]})
    await execute_job(
        allowed_job,
        config=config,
        store=store,
        router=cast(Any, object()),
        agent=cast(Any, FakeAgent()),
    )
    assert seen == [False, True]


@pytest.mark.asyncio
async def test_execute_job_timeout_returns_a_bounded_record(
    config, store: Store, sample_repo: Path
):
    class SlowAgent:
        async def run(self, *_args, **_kwargs):
            await asyncio.sleep(30)

    job = _job(sample_repo, prompt="Hang.", next_run_at=None, timeout_s=0.05)
    record = await execute_job(
        job,
        config=config,
        store=store,
        router=cast(Any, object()),
        agent=cast(Any, SlowAgent()),
    )
    assert record.status == "timeout"
    assert record.session_id is not None
    assert store.get_session(record.session_id).status == "idle"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_execute_job_missing_workspace_fails_cleanly(config, store: Store, tmp_path: Path):
    job = _job(tmp_path / "missing", next_run_at=None)
    record = await execute_job(job, config=config, store=store, router=cast(Any, object()))
    assert record.status == "error"
    assert "workspace does not exist" in (record.error or "")


async def _fake_executor(job: CronJob, **_kwargs) -> CronRun:
    now = _now().isoformat()
    return CronRun(
        started_at=now,
        finished_at=now,
        status="ok",
        session_id="ses_cron",
        steps=2,
        allowance=0.05,
    )


@pytest.mark.asyncio
async def test_scheduler_start_is_opt_in(config: Config, store: Store, tmp_path: Path):
    scheduler = CronScheduler(
        config.model_copy(update={"cron": CronConfig(enabled=False)}),
        store,
        cast(Any, object()),
        cron_store=CronStore(tmp_path),
        executor=_fake_executor,
    )
    await scheduler.start()
    assert scheduler._task is None

    enabled = CronScheduler(
        config.model_copy(update={"cron": CronConfig(enabled=True, tick_seconds=3600)}),
        store,
        cast(Any, object()),
        cron_store=CronStore(tmp_path),
        executor=_fake_executor,
    )
    await enabled.start()
    assert enabled._task is not None
    await enabled.stop()
    assert enabled._task is None


@pytest.mark.asyncio
async def test_scheduler_runs_due_jobs_and_records_usage(
    config: Config, store: Store, tmp_path: Path
):
    cron_store = CronStore(tmp_path)
    job = _job(tmp_path)
    cron_store.add(job)
    scheduler = CronScheduler(
        config.model_copy(update={"cron": CronConfig(enabled=True, catch_up=True)}),
        store,
        object(),  # type: ignore[arg-type]
        cron_store=cron_store,
        executor=_fake_executor,
    )
    records = await scheduler.tick()
    assert len(records) == 1
    updated = cron_store.get(job.id)
    assert updated is not None
    assert updated.last_status == "ok"
    assert updated.last_session_id == "ses_cron"
    assert updated.runs[-1].allowance == pytest.approx(0.05)
    assert cron_store.used_today() == pytest.approx(0.05)
    assert updated.next_run_at is not None
    assert datetime.fromisoformat(updated.next_run_at) > _now()


@pytest.mark.asyncio
async def test_scheduler_skips_missed_jobs_without_catch_up(
    config: Config, store: Store, tmp_path: Path
):
    cron_store = CronStore(tmp_path)
    job = _job(tmp_path, next_run_at=(_now() - timedelta(minutes=10)).isoformat())
    cron_store.add(job)
    calls: list[str] = []

    async def executor(item: CronJob, **_kwargs) -> CronRun:
        calls.append(item.id)
        return await _fake_executor(item)

    scheduler = CronScheduler(
        config.model_copy(update={"cron": CronConfig(enabled=True, catch_up=False)}),
        store,
        object(),  # type: ignore[arg-type]
        cron_store=cron_store,
        executor=executor,
    )
    assert await scheduler.tick() == []
    assert calls == []
    updated = cron_store.get(job.id)
    assert updated is not None
    assert datetime.fromisoformat(updated.next_run_at or "") > _now()  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_scheduler_stops_at_the_daily_allowance(config: Config, store: Store, tmp_path: Path):
    cron_store = CronStore(tmp_path)
    job = _job(tmp_path)
    cron_store.add(job)
    cron_store.update_job(job.id, lambda _item: None, allowance=2.0)
    calls: list[str] = []

    async def executor(item: CronJob, **_kwargs) -> CronRun:
        calls.append(item.id)
        return await _fake_executor(item)

    scheduler = CronScheduler(
        config.model_copy(
            update={"cron": CronConfig(enabled=True, catch_up=True, max_daily_allowance=2.0)}
        ),
        store,
        object(),  # type: ignore[arg-type]
        cron_store=cron_store,
        executor=executor,
    )
    assert await scheduler.tick() == []
    assert calls == []
    updated = cron_store.get(job.id)
    assert updated is not None
    assert updated.last_error == "daily allowance exhausted"


@pytest.mark.asyncio
async def test_scheduler_does_not_overlap_a_manual_run(
    config: Config, store: Store, tmp_path: Path
):
    cron_store = CronStore(tmp_path)
    job = _job(tmp_path)
    cron_store.add(job)
    scheduler = CronScheduler(
        config.model_copy(update={"cron": CronConfig(enabled=True, catch_up=True)}),
        store,
        object(),  # type: ignore[arg-type]
        cron_store=cron_store,
        executor=_fake_executor,
    )
    with run_lock(tmp_path, timeout_s=30):
        with pytest.raises(CronBusy), run_lock(tmp_path, timeout_s=30):
            pass
        assert await scheduler.tick() == []


def test_cron_cli_add_list_disable_enable_and_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SIWAH_CONFIG", raising=False)
    runner = CliRunner()
    added = runner.invoke(
        app,
        [
            "cron",
            "add",
            "--schedule",
            "0 9 * * *",
            "--cwd",
            str(tmp_path),
            "--prompt",
            "Nightly tests",
            "--model",
            "mock/model-a",
            "--allow",
            "bash",
            "--id",
            "nightly",
        ],
    )
    assert added.exit_code == 0, added.output
    assert "Added cron job nightly" in added.output
    assert "Automatic runs are off" in added.output

    listed = runner.invoke(app, ["cron", "list", "--json"])
    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.output)
    assert payload["jobs"][0]["id"] == "nightly"
    assert payload["jobs"][0]["allow"] == ["bash"]

    disabled = runner.invoke(app, ["cron", "disable", "nightly"])
    assert disabled.exit_code == 0, disabled.output
    assert (
        json.loads((tmp_path / "home" / "harness" / "cron.json").read_text())["jobs"][0]["enabled"]
        is False
    )

    enabled = runner.invoke(app, ["cron", "enable", "nightly"])
    assert enabled.exit_code == 0, enabled.output

    removed = runner.invoke(app, ["cron", "remove", "nightly"])
    assert removed.exit_code == 0, removed.output
    empty = runner.invoke(app, ["cron", "list"])
    assert "No cron jobs" in empty.output


def test_cron_cli_rejects_bad_schedule_and_unknown_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SIWAH_CONFIG", raising=False)
    runner = CliRunner()
    bad = runner.invoke(
        app,
        [
            "cron",
            "add",
            "--schedule",
            "every day",
            "--cwd",
            str(tmp_path),
            "--prompt",
            "x",
            "--model",
            "mock/model-a",
        ],
    )
    assert bad.exit_code == 2
    assert "5 fields" in bad.output
    unknown = runner.invoke(
        app,
        [
            "cron",
            "add",
            "--schedule",
            "0 9 * * *",
            "--cwd",
            str(tmp_path),
            "--prompt",
            "x",
            "--model",
            "mock/model-a",
            "--allow",
            "root",
        ],
    )
    assert unknown.exit_code == 2
    assert "unknown permission kind" in unknown.output


def test_cron_is_opt_in_by_default():
    assert Config().cron.enabled is False
    assert Config().cron.max_daily_allowance > 0
