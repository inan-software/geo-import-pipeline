import asyncio
from pathlib import Path

import pytest

from geo_import_pipeline import scheduler as scheduler_module
from geo_import_pipeline.config import Settings, SourceConfig
from geo_import_pipeline.scheduler import build_scheduler


def settings():
    return Settings(
        db_host="x", db_port=5432, db_name="x", db_user="x", db_password="x",
        config_dir=Path("."), report_dir=Path("."), cache_dir=Path("."), migrations_dir=Path("."),
        user_agent="t", timezone="UTC", plugin_modules=(),
    )  # fmt: skip


def source(source_id, schedule="0 3 1 * *", enabled=True):
    return SourceConfig.model_validate(
        {
            "id": source_id,
            "type": "csv",
            "schedule": schedule,
            "enabled": enabled,
            "key": ["k"],
            "fields": {"k": {"path": "k"}},
            "csv": {"path": "x.csv"},
            "target": {"kind": "attributes", "table": "s.t", "columns": {}, "source_column": "c", "source_value": "v"},
        }
    )


def test_only_enabled_sources_with_a_schedule_become_jobs():
    async def work():
        scheduler = build_scheduler(
            settings(), [source("a"), source("b", enabled=False), source("c", schedule=None)]
        )
        return [job.id for job in scheduler.get_jobs()]

    assert asyncio.run(work()) == ["a"]


def test_each_source_keeps_its_own_cron_expression():
    async def work():
        scheduler = build_scheduler(settings(), [source("a", "0 3 1 * *"), source("b", "30 4 2 * *")])
        return {job.id: str(job.trigger) for job in scheduler.get_jobs()}

    triggers = asyncio.run(work())
    assert "hour='3'" in triggers["a"] and "minute='30'" in triggers["b"] and "day='2'" in triggers["b"]


def test_simultaneous_jobs_run_one_after_the_other(monkeypatch):
    active, peak, seen = [0], [0], []

    async def fake_execute_run(_settings, sources, trigger):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        await asyncio.sleep(0)
        seen.append((sources[0].id, trigger))
        active[0] -= 1
        return {}, ""

    monkeypatch.setattr(scheduler_module, "execute_run", fake_execute_run)

    async def work():
        scheduler = build_scheduler(settings(), [source("a"), source("b")])
        jobs = scheduler.get_jobs()
        await asyncio.gather(*(job.func(*job.args) for job in jobs))

    asyncio.run(work())
    assert peak[0] == 1
    assert sorted(seen) == [("a", "schedule"), ("b", "schedule")]


def test_a_crashing_run_does_not_escape_into_the_scheduler(monkeypatch):
    async def fake_execute_run(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(scheduler_module, "execute_run", fake_execute_run)

    async def work():
        job = build_scheduler(settings(), [source("a")]).get_jobs()[0]
        await job.func(*job.args)

    asyncio.run(work())  # Must not raise.


def test_invalid_cron_is_rejected_at_load_time():
    with pytest.raises(ValueError):
        source("a", schedule="every monday")
