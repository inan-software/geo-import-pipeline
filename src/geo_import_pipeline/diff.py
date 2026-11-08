"""Business-level comparison of a fetch with the last imported state.

Runs before the replacement. new = key was not there, changed = key exists with
different content, deleted = key is no longer delivered. Compares against
pipeline_meta.source_state, because parts of a key may not exist in the target tables.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import asyncpg

from .staging import Staging

SAMPLE_SIZE = 50
STATE = "pipeline_meta.source_state"


@dataclass(slots=True)
class DiffResult:
    new: int = 0
    changed: int = 0
    deleted: int = 0
    baseline: bool = False
    samples: dict[str, list[str]] = field(default_factory=dict)


async def compute_diff(conn: asyncpg.Connection, source_id: str, staging: Staging) -> DiffResult:
    join = f"FROM {staging.table} s LEFT JOIN {STATE} t ON t.source_id = $1 AND t.key_hash = s.key_hash"
    missing = f"FROM {STATE} t LEFT JOIN {staging.table} s ON s.key_hash = t.key_hash WHERE t.source_id = $1 AND s.key_hash IS NULL"
    conditions = {"new": "t.key_hash IS NULL", "changed": "t.key_hash IS NOT NULL AND t.content_hash <> s.content_hash"}

    result = DiffResult()
    result.baseline = not await conn.fetchval(f"SELECT EXISTS (SELECT 1 FROM {STATE} WHERE source_id = $1)", source_id)
    for name, condition in conditions.items():
        setattr(result, name, await conn.fetchval(f"SELECT count(*) {join} WHERE {condition}", source_id))
        rows = await conn.fetch(f"SELECT s.key_text {join} WHERE {condition} LIMIT {SAMPLE_SIZE}", source_id)
        result.samples[name] = [row["key_text"] for row in rows]
    result.deleted = await conn.fetchval(f"SELECT count(*) {missing}", source_id)
    rows = await conn.fetch(f"SELECT t.key_text {missing} LIMIT {SAMPLE_SIZE}", source_id)
    result.samples["deleted"] = [row["key_text"] for row in rows]
    return result
