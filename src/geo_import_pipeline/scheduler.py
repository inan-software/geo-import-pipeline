"""Time-controlled runs. Every source has its own cron expression."""

from __future__ import annotations

import asyncio
import logging
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import ConfigError, Settings, SourceConfig, load_sources
from .db import connect, migrate
from .runner import execute_run

log = logging.getLogger(__name__)
MISFIRE_GRACE_S = 3600


def build_scheduler(settings: Settings, sources: list[SourceConfig]) -> AsyncIOScheduler:
    """One job per enabled source with a schedule. The lock keeps runs strictly sequential."""
    lock = asyncio.Lock()

    async def job(source: SourceConfig) -> None:
        async with lock:
            try:
                await execute_run(settings, [source], trigger="schedule")
            except Exception:  # The scheduler has to survive anything a run does.
                log.exception("Scheduled run of %s failed", source.id)

    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    for source in sources:
        if source.enabled and source.schedule:
            scheduler.add_job(
                job,
                CronTrigger.from_crontab(source.schedule, timezone=settings.timezone),
                args=[source],
                id=source.id,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=MISFIRE_GRACE_S,
            )
    return scheduler


async def run_scheduler(settings: Settings) -> int:
    sources = list(load_sources(settings.config_dir).values())
    conn = await connect(settings)
    try:
        await migrate(conn, settings.migrations_dir)
    finally:
        await conn.close()

    scheduler = build_scheduler(settings, sources)
    jobs = scheduler.get_jobs()
    if not jobs:
        raise ConfigError("No enabled source has a schedule")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for number in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(number, stop.set)

    scheduler.start()
    for job in scheduler.get_jobs():
        log.info("Scheduled %s, next run %s", job.id, job.next_run_time)
    await stop.wait()
    # An import cut off here is rolled back by the database, so no half state remains.
    log.info("Shutting down")
    scheduler.shutdown(wait=False)
    return 0
