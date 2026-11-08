"""Staging tables in pipeline_meta and the geometry processing in PostGIS."""

from __future__ import annotations

import hashlib

import asyncpg

from .config import SourceConfig
from .db import quote_ident
from .model import IncompleteFetchError, StagedRow, SourceStats

SCHEMA = "pipeline_meta"
COPY_COLUMNS = ("row_no", "key_hash", "key_text", "content_hash", "attrs", "gml")

GEOMETRY_WARNINGS = {
    "parse_error": "Geometry could not be read and was skipped",
    "empty": "Geometry is empty and was skipped",
    "transform_error": "Geometry could not be reprojected and was skipped",
    "invalid": "Invalid geometry could not be repaired and was skipped",
    "type_mismatch": "Geometry does not fit the target type and was skipped",
    "missing": "Record has no geometry and was skipped",
}


def _prefix(source_id: str) -> str:
    return "stg_" + hashlib.sha1(source_id.encode("utf-8")).hexdigest()[:12]


class Staging:
    def __init__(self, conn: asyncpg.Connection, source: SourceConfig, run_id: str) -> None:
        self.conn = conn
        self.source = source
        self.name = f"{_prefix(source.id)}_{run_id[:8]}"
        self.table = f"{SCHEMA}.{quote_ident(self.name)}"

    async def create(self) -> None:
        # Remove leftovers of a crashed run. Safe because the source lock is held.
        leftovers = await self.conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = $1 AND tablename LIKE $2",
            SCHEMA,
            _prefix(self.source.id) + "\\_%",
        )
        for row in leftovers:
            await self.conn.execute(f"DROP TABLE {SCHEMA}.{quote_ident(row['tablename'])}")
        await self.conn.execute(
            f"""CREATE UNLOGGED TABLE {self.table} (
                    row_no       bigint NOT NULL,
                    key_hash     bytea  NOT NULL,
                    key_text     text   NOT NULL,
                    content_hash bytea  NOT NULL,
                    attrs        jsonb  NOT NULL,
                    gml          text,
                    geom         geometry
                )"""
        )

    async def write(self, rows: list[StagedRow]) -> None:
        if not rows:
            return
        await self.conn.copy_records_to_table(
            self.name,
            schema_name=SCHEMA,
            columns=COPY_COLUMNS,
            records=[(r.row_no, r.key_hash, r.key_text, r.content_hash, r.attrs, r.gml) for r in rows],
        )

    async def resolve_duplicates(self, stats: SourceStats) -> None:
        """A key may occur only once per fetch. Keep the first row, or fail, per configuration."""
        await self.conn.execute(f"CREATE INDEX ON {self.table} (key_hash, row_no)")
        await self.conn.execute(f"ANALYZE {self.table}")
        duplicates = await self.conn.fetchval(f"SELECT count(*) - count(DISTINCT key_hash) FROM {self.table}")
        if not duplicates:
            return
        sample = await self.conn.fetchval(
            f"SELECT key_text FROM {self.table} GROUP BY key_text HAVING count(*) > 1 LIMIT 1"
        )
        if self.source.on_duplicate == "fail":
            raise IncompleteFetchError(
                f"{duplicates} duplicate key value(s), e.g. '{sample}'. "
                "With unstable paging records may also be missing."
            )
        await self.conn.execute(
            f"""DELETE FROM {self.table} t USING (
                    SELECT row_no, row_number() OVER (PARTITION BY key_hash ORDER BY row_no) AS position
                    FROM {self.table}
                ) d
                WHERE t.row_no = d.row_no AND d.position > 1"""
        )
        stats.warnings.add("duplicate_key", "Duplicate key, kept the first record", sample, duplicates)
        stats.skipped += duplicates

    async def process_geometries(self, stats: SourceStats) -> None:
        """Run every GML fragment through PostGIS and drop what cannot be used."""
        target = self.source.target
        await self.conn.execute(
            f"""UPDATE {self.table} s
                SET geom = p.out_geom,
                    gml = NULL,
                    attrs = s.attrs || jsonb_build_object('__geom_status', p.out_status),
                    content_hash = CASE WHEN p.out_hash IS NULL THEN s.content_hash
                                        ELSE sha256(s.content_hash || p.out_hash) END
                FROM {self.table} src
                CROSS JOIN LATERAL pipeline_meta.process_gml(src.gml, $1, $2, $3) p
                WHERE s.row_no = src.row_no AND src.gml IS NOT NULL""",
            self.source.wfs.fallback_srid,
            target.geom_type,
            target.srid,
        )
        rows = await self.conn.fetch(
            f"""SELECT COALESCE(attrs->>'__geom_status', 'missing') AS status,
                       count(*) AS amount, array_agg(key_text ORDER BY row_no) AS keys
                FROM {self.table} GROUP BY 1"""
        )
        for row in rows:
            status, amount, keys = row["status"], row["amount"], row["keys"]
            if status == "ok":
                continue
            if status == "repaired":
                stats.warnings.add("geometry_repaired", "Invalid geometry was repaired with ST_MakeValid", None, amount)
                for key in keys[:20]:
                    stats.warnings.add("geometry_repaired", "Invalid geometry was repaired with ST_MakeValid", key, 0)
                continue
            message = GEOMETRY_WARNINGS.get(status, f"Geometry skipped: {status}")
            stats.warnings.add(f"geometry_{status}", message, None, amount)
            for key in keys[:20]:
                stats.warnings.add(f"geometry_{status}", message, key, 0)
            stats.skipped += amount
        await self.conn.execute(
            f"DELETE FROM {self.table} WHERE COALESCE(attrs->>'__geom_status', 'missing') NOT IN ('ok', 'repaired')"
        )
        await self.conn.execute(f"UPDATE {self.table} SET attrs = attrs - '__geom_status'")

    async def count(self) -> int:
        return await self.conn.fetchval(f"SELECT count(*) FROM {self.table}")

    async def drop(self) -> None:
        await self.conn.execute(f"DROP TABLE IF EXISTS {self.table}")
