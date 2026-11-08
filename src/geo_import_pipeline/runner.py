"""Processing of one source: lock, fetch, stage, geometry, replace."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field

import asyncpg
import httpx

from .config import Settings, SourceConfig
from .load import inspect_target, replace_source
from .model import FetchError, SourceError, SourceStats, StagedRow
from .sources import adapter_for
from .staging import Staging
from .transform import Transformer

log = logging.getLogger(__name__)
BATCH_SIZE = 2000


@dataclass
class SourceResult:
    source_id: str
    status: str = "failed"
    error: str | None = None
    stats: SourceStats = field(default_factory=SourceStats)
    version: str | None = None


async def _fetch_into(staging: Staging, source: SourceConfig, adapter, stats: SourceStats) -> None:
    transformer = Transformer(source, stats.warnings)
    batch: list[StagedRow] = []
    async for raw in adapter.records():
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


async def run_source(
    conn: asyncpg.Connection, source: SourceConfig, settings: Settings, client: httpx.AsyncClient, run_id: str | None = None
) -> SourceResult:
    run_id = run_id or str(uuid.uuid4())
    result = SourceResult(source_id=source.id)
    stats = result.stats
    locked = await conn.fetchval("SELECT pg_try_advisory_lock(hashtext($1))", source.id)
    if not locked:
        result.error = "Another run of this source is active"
        return result

    staging = Staging(conn, source, run_id)
    try:
        info = await inspect_target(conn, source)
        await staging.create()
        adapter = adapter_for(source, client, stats, settings.cache_dir)
        await _fetch_into(staging, source, adapter, stats)
        result.version = adapter.version
        await staging.resolve_duplicates(stats)
        if source.type == "wfs":
            await staging.process_geometries(stats)
        stats.written = await replace_source(
            conn, source, info, staging, run_id, result.version, datetime.now(timezone.utc)
        )
        result.status = "success"
    except SourceError as exc:
        result.error = str(exc)
    finally:
        await staging.drop()
        await conn.execute("SELECT pg_advisory_unlock(hashtext($1))", source.id)
    return result
