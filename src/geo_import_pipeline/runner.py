"""The sequence for one source and a whole run, with failures isolated per source."""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import aclosing
from datetime import datetime, timezone

import asyncpg
import httpx

from .config import Settings, SourceConfig
from .db import connect, migrate
from .diff import compute_diff
from .load import inspect_target, replace_source
from .model import IncompleteFetchError, SourceError, SourceStats, StagedRow
from .report import build_report, write_report
from .runner_types import SourceResult
from .sources import adapter_for
from .staging import Staging
from .transform import Transformer

log = logging.getLogger(__name__)
BATCH_SIZE = 2000


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _fetch_into(staging: Staging, source: SourceConfig, adapter, stats: SourceStats) -> None:
    transformer = Transformer(source, stats.warnings)
    batch: list[StagedRow] = []
    async with aclosing(adapter.records()) as records:  # Releases files and processes on errors.
        async for raw in records:
            if not transformer.matches(raw):
                stats.filtered_out += 1
                continue
            row = transformer.apply(raw)
            if row is None:
                stats.skipped += 1
                continue
            batch.append(row)
            if len(batch) >= BATCH_SIZE:
                await staging.write(batch)
                batch.clear()
    await staging.write(batch)


async def _process(conn, source, settings, client, run_id, result: SourceResult, staging: Staging) -> None:
    stats = result.stats
    info = await inspect_target(conn, source)
    await staging.create()
    adapter = adapter_for(source, client, stats, settings.cache_dir)
    await _fetch_into(staging, source, adapter, stats)
    result.version = adapter.version
    result.fetched_at = _now()
    await staging.resolve_duplicates(stats)
    if source.type == "wfs":
        await staging.process_geometries(stats)
    result.valid = await staging.count()
    if result.valid < source.min_rows:
        raise IncompleteFetchError(f"Only {result.valid} usable row(s), at least {source.min_rows} expected")

    # Everything up to here only touched the staging table.
    result.diff = await compute_diff(conn, source.id, staging)
    stats.written = await replace_source(conn, source, info, staging, run_id, result.version, _now())
    result.status = "success"


async def run_source(
    conn: asyncpg.Connection,
    source: SourceConfig,
    settings: Settings,
    client: httpx.AsyncClient,
    run_id: str | None = None,
) -> SourceResult:
    """Import one source. Never raises for a failing source; the result says what happened."""
    run_id = run_id or str(uuid.uuid4())
    result = SourceResult(
        source_id=source.id, source_type=source.type, target_kind=source.target.kind, started_at=_now()
    )
    locked = False
    staging = Staging(conn, source, run_id)
    try:
        locked = await conn.fetchval("SELECT pg_try_advisory_lock(hashtext($1))", source.id)
        if not locked:
            raise SourceError("Another run of this source is active")
        await _process(conn, source, settings, client, run_id, result, staging)
    except SourceError as exc:
        result.error = str(exc)
        result.diff = None
        log.error("%s failed: %s", source.id, exc)
    except Exception as exc:  # A bug or data problem in one source must not stop the next one.
        result.error = f"{type(exc).__name__}: {exc}"
        result.diff = None
        log.exception("%s failed unexpectedly", source.id)
    finally:
        if locked:
            try:
                await staging.drop()
                await conn.execute("SELECT pg_advisory_unlock(hashtext($1))", source.id)
            except Exception:
                log.warning("Cleanup after %s failed", source.id, exc_info=True)
        result.finished_at = _now()
    return result


async def _protocol_start(conn, run_id: str, trigger: str, started_at: datetime) -> None:
    await conn.execute(
        "INSERT INTO pipeline_meta.runs (run_id, trigger, started_at, status) VALUES ($1::uuid, $2, $3, 'running')",
        run_id,
        trigger,
        started_at,
    )


async def _protocol_source(conn, run_id: str, r: SourceResult) -> None:
    diff = r.diff
    await conn.execute(
        """INSERT INTO pipeline_meta.source_runs
           (run_id, source_id, started_at, finished_at, status, source_version, fetched_rows, valid_rows,
            skipped_rows, new_rows, changed_rows, deleted_rows, baseline, error, warnings)
           VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15::jsonb)""",
        run_id,
        r.source_id,
        r.started_at,
        r.finished_at,
        r.status,
        r.version,
        r.stats.read,
        r.valid,
        r.stats.skipped,
        diff.new if diff else 0,
        diff.changed if diff else 0,
        diff.deleted if diff else 0,
        diff.baseline if diff else False,
        r.error,
        json.dumps(r.stats.warnings.as_list(), ensure_ascii=False),
    )


async def _protocol_finish(conn, run_id: str, finished_at: datetime, status: str, path: str) -> None:
    await conn.execute(
        "UPDATE pipeline_meta.runs SET finished_at = $2, status = $3, report_path = $4 WHERE run_id = $1::uuid",
        run_id,
        finished_at,
        status,
        path,
    )


async def _safely(description: str, operation) -> None:
    """The run history is secondary. Losing it must not cost the report or other sources."""
    try:
        await operation
    except Exception:
        log.warning("Could not write %s to the run history", description, exc_info=True)


async def _run_one(source, settings, client, run_id) -> SourceResult:
    """Each source gets its own connection, so a lost connection only affects that source."""
    try:
        conn = await connect(settings)
    except Exception as exc:
        now = _now()
        return SourceResult(
            source_id=source.id,
            source_type=source.type,
            target_kind=source.target.kind,
            error=f"Database connection failed: {exc}",
            started_at=now,
            finished_at=now,
        )
    try:
        result = await run_source(conn, source, settings, client, run_id)
        await _safely(f"source {source.id}", _protocol_source(conn, run_id, result))
        return result
    finally:
        await conn.close()


async def execute_run(
    settings: Settings,
    sources: list[SourceConfig],
    trigger: str = "manual",
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[dict, str]:
    """Import the sources one after the other and always write a report."""
    run_id = str(uuid.uuid4())
    started_at = _now()
    meta: asyncpg.Connection | None = None
    try:
        meta = await connect(settings)
        await migrate(meta, settings.migrations_dir)
        await _safely("run start", _protocol_start(meta, run_id, trigger, started_at))
    except Exception:
        log.exception("Control schema is not available")
        if meta is not None:
            await meta.close()
        meta = None

    results: list[SourceResult] = []
    headers = {"User-Agent": settings.user_agent}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, transport=transport) as client:
        for source in sources:  # Deliberately sequential: no parallel bulk requests to the services.
            log.info("Starting source %s", source.id)
            results.append(await _run_one(source, settings, client, run_id))

    finished_at = _now()
    report = build_report(run_id, trigger, started_at, finished_at, results)
    path = write_report(settings.report_dir, report)
    log.info("Report written to %s", path)
    if meta is not None:
        await _safely("run end", _protocol_finish(meta, run_id, finished_at, report["status"], str(path)))
        await meta.close()
    return report, str(path)
