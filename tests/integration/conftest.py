"""Each test gets its own throwaway database with migrations and example target tables."""

import asyncio
import dataclasses
import uuid
from pathlib import Path

import asyncpg
import pytest

from geo_import_pipeline.config import Settings
from geo_import_pipeline.db import connect, migrate

ROOT = Path(__file__).parents[2]


@pytest.fixture
def settings(tmp_path):
    base = Settings.from_env()
    name = f"gip_test_{uuid.uuid4().hex[:12]}"

    async def admin(sql):
        conn = await asyncpg.connect(
            host=base.db_host, port=base.db_port, database=base.db_name, user=base.db_user, password=base.db_password
        )
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(admin(f'CREATE DATABASE "{name}"'))
    current = dataclasses.replace(
        base,
        db_name=name,
        config_dir=tmp_path / "config",
        report_dir=tmp_path / "reports",
        cache_dir=tmp_path / "cache",
        migrations_dir=ROOT / "migrations",
    )

    async def prepare():
        conn = await connect(current)
        try:
            await migrate(conn, current.migrations_dir)
            await conn.execute((ROOT / "examples" / "target_schema.sql").read_text(encoding="utf-8"))
        finally:
            await conn.close()

    asyncio.run(prepare())
    yield current
    asyncio.run(admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture
def db(settings):
    """Run one coroutine against the test database."""

    def run(work):
        async def wrapper():
            conn = await connect(settings)
            try:
                return await work(conn)
            finally:
                await conn.close()

        return asyncio.run(wrapper())

    return run
