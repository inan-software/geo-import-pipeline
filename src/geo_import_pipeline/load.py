"""Replacement of the data in the target tables, one transaction per source.

This module only issues DML on target tables. It never alters their structure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import asyncpg

from .config import SourceConfig, split_table
from .db import quote_ident as qi, quote_table
from .model import TargetError
from .staging import Staging


@dataclass(frozen=True)
class TargetInfo:
    column_types: dict[str, str]
    metadata_types: dict[str, str]


async def _column_types(conn: asyncpg.Connection, table: str) -> dict[str, str]:
    schema, name = split_table(table)
    rows = await conn.fetch(
        """SELECT a.attname, format_type(a.atttypid, a.atttypmod) AS type
           FROM pg_attribute a
           JOIN pg_class c ON c.oid = a.attrelid
           JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = $1 AND c.relname = $2 AND a.attnum > 0 AND NOT a.attisdropped""",
        schema,
        name,
    )
    return {row["attname"]: row["type"] for row in rows}


def _check_columns(table: str, available: dict[str, str], required: list[str]) -> None:
    if not available:
        raise TargetError(f"Target table {table} does not exist")
    missing = [column for column in required if column not in available]
    if missing:
        raise TargetError(f"Target table {table} has no column(s): {', '.join(missing)}")


async def inspect_target(conn: asyncpg.Connection, source: SourceConfig) -> TargetInfo:
    """Compare the configuration with the real tables before anything is fetched."""
    target = source.target
    types = await _column_types(conn, target.table)
    required = list(target.columns)
    if target.geom_column:
        required.append(target.geom_column)
    if target.source_column:
        required.append(target.source_column)
    _check_columns(target.table, types, required)

    if target.geom_column:
        actual = types[target.geom_column].replace(" ", "").lower()
        expected = f"geometry({target.geom_type},{target.srid})".lower()
        if actual not in (expected, "geometry"):
            raise TargetError(f"Column {target.geom_column} is {types[target.geom_column]}, expected {expected}")

    metadata_types: dict[str, str] = {}
    if target.metadata:
        metadata_types = await _column_types(conn, target.metadata.table)
        _check_columns(
            target.metadata.table, metadata_types, [*target.metadata.key, *target.metadata.values]
        )
    return TargetInfo(column_types=types, metadata_types=metadata_types)


def _value_sql(source: SourceConfig, info: TargetInfo, column: str, alias: str = "s") -> str:
    field_name = source.target.columns[column].replace("'", "''")
    return f"({alias}.attrs->>'{field_name}')::{info.column_types[column]}"


async def _replace_attributes(conn: asyncpg.Connection, source: SourceConfig, info: TargetInfo, staging: Staging) -> int:
    target = source.target
    table = quote_table(target.table)
    columns = list(target.columns)
    names = ", ".join([qi(c) for c in columns] + [qi(target.source_column)])
    values = ", ".join(_value_sql(source, info, c) for c in columns)
    await conn.execute(f"DELETE FROM {table} WHERE {qi(target.source_column)} = $1", target.source_value)
    status = await conn.execute(
        f"INSERT INTO {table} ({names}) SELECT {values}, $1 FROM {staging.table} s",
        target.source_value,
    )
    return int(status.split()[-1])


async def _upsert_geometries(conn: asyncpg.Connection, source: SourceConfig, info: TargetInfo, staging: Staging) -> int:
    """Update by key and insert what is new. Rows are never deleted: geometries are shared."""
    target = source.target
    table = quote_table(target.table)
    key_columns = [c for c, name in target.columns.items() if name in source.key]
    other_columns = [c for c in target.columns if c not in key_columns]
    match = " AND ".join(f"t.{qi(c)} = {_value_sql(source, info, c)}" for c in key_columns)

    assignments = [f"{qi(c)} = {_value_sql(source, info, c)}" for c in other_columns]
    assignments.append(f"{qi(target.geom_column)} = s.geom")
    updated = await conn.execute(f"UPDATE {table} t SET {', '.join(assignments)} FROM {staging.table} s WHERE {match}")

    columns = key_columns + other_columns
    names = ", ".join([qi(c) for c in columns] + [qi(target.geom_column)])
    values = ", ".join(_value_sql(source, info, c) for c in columns)
    inserted = await conn.execute(
        f"""INSERT INTO {table} ({names})
            SELECT {values}, s.geom FROM {staging.table} s
            WHERE NOT EXISTS (SELECT 1 FROM {table} t WHERE {match})"""
    )
    return int(updated.split()[-1]) + int(inserted.split()[-1])


async def _update_state(conn: asyncpg.Connection, source: SourceConfig, staging: Staging, run_id: str) -> None:
    await conn.execute("DELETE FROM pipeline_meta.source_state WHERE source_id = $1", source.id)
    await conn.execute(
        f"""INSERT INTO pipeline_meta.source_state (source_id, key_hash, key_text, content_hash, last_run_id)
            SELECT $1, key_hash, key_text, content_hash, $2::uuid FROM {staging.table}""",
        source.id,
        run_id,
    )


async def _update_metadata(
    conn: asyncpg.Connection, source: SourceConfig, info: TargetInfo, version: str | None, now: datetime
) -> None:
    metadata = source.target.metadata
    if metadata is None:
        return
    table = quote_table(metadata.table)
    context = {"source_id": source.id, "run_date": now.date().isoformat(), "version": version or ""}
    pairs = {column: value.format_map(context) for column, value in metadata.values.items()}

    def cast(column: str, position: int) -> str:
        return f"${position}::text::{info.metadata_types[column]}"

    key_items = list(metadata.key.items())
    value_items = list(pairs.items())
    # Timestamps of the run are written to a column named "modified" when the table has one.
    extra_set, extra_args = "", []
    extra_insert_col, extra_insert_val = "", ""
    if "modified" in info.metadata_types and "modified" not in pairs:
        position = len(key_items) + len(value_items) + 1
        extra_set = f", {qi('modified')} = ${position}" if value_items else f"{qi('modified')} = ${position}"
        extra_insert_col, extra_insert_val = f", {qi('modified')}", f", ${position}"
        extra_args = [now]

    key_args = [value for _, value in key_items]
    value_args = [value for _, value in value_items]
    where = " AND ".join(f"{qi(c)} = {cast(c, i + 1)}" for i, (c, _) in enumerate(key_items))
    offset = len(key_items)
    assignments = ", ".join(f"{qi(c)} = {cast(c, offset + i + 1)}" for i, (c, _) in enumerate(value_items))
    args = [*key_args, *value_args, *extra_args]

    result = "UPDATE 0"
    if assignments or extra_set:
        result = await conn.execute(f"UPDATE {table} SET {assignments}{extra_set} WHERE {where}", *args)
    if result.endswith(" 0"):
        names = ", ".join(qi(c) for c, _ in [*key_items, *value_items])
        placeholders = ", ".join(cast(c, i + 1) for i, (c, _) in enumerate([*key_items, *value_items]))
        await conn.execute(
            f"INSERT INTO {table} ({names}{extra_insert_col}) VALUES ({placeholders}{extra_insert_val})", *args
        )


async def replace_source(
    conn: asyncpg.Connection,
    source: SourceConfig,
    info: TargetInfo,
    staging: Staging,
    run_id: str,
    version: str | None,
    now: datetime,
) -> int:
    """Swap the data of one source atomically. Readers never see a half-finished state."""
    async with conn.transaction():
        if source.target.kind == "attributes":
            written = await _replace_attributes(conn, source, info, staging)
        else:
            written = await _upsert_geometries(conn, source, info, staging)
        await _update_state(conn, source, staging, run_id)
        await _update_metadata(conn, source, info, version, now)
    return written
