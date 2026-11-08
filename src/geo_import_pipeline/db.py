"""Database access and the migrations of the control schema."""

from __future__ import annotations

import logging
from pathlib import Path

import asyncpg

from .config import Settings, split_table

log = logging.getLogger(__name__)

# Serialises migrations when several containers start at once.
MIGRATION_LOCK = 0x47495000


async def connect(settings: Settings) -> asyncpg.Connection:
    return await asyncpg.connect(
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
    )


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def quote_table(name: str) -> str:
    schema, table = split_table(name)
    return f"{quote_ident(schema)}.{quote_ident(table)}"


async def migrate(conn: asyncpg.Connection, migrations_dir: Path) -> list[str]:
    """Apply pending migrations. Only ever touches the schema ``pipeline_meta``."""
    applied_now: list[str] = []
    await conn.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK)
    try:
        await conn.execute("CREATE SCHEMA IF NOT EXISTS pipeline_meta")
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS pipeline_meta.schema_migrations (
                   version text PRIMARY KEY,
                   applied_at timestamptz NOT NULL DEFAULT now()
               )"""
        )
        done = {r["version"] for r in await conn.fetch("SELECT version FROM pipeline_meta.schema_migrations")}
        for path in sorted(migrations_dir.glob("*.sql")):
            if path.name in done:
                continue
            log.info("Applying migration %s", path.name)
            async with conn.transaction():
                await conn.execute(path.read_text(encoding="utf-8"))
                await conn.execute("INSERT INTO pipeline_meta.schema_migrations (version) VALUES ($1)", path.name)
            applied_now.append(path.name)
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK)
    return applied_now
