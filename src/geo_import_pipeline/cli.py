"""Command line interface."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

import httpx

from .config import ConfigError, Settings, load_sources
from .db import connect, migrate
from .runner import run_source


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="geo-import-pipeline", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list-sources", help="Show the configured sources")
    commands.add_parser("validate-config", help="Validate all source definitions and exit")
    commands.add_parser("migrate", help="Create or update the schema pipeline_meta")
    run = commands.add_parser("run", help="Import one source or all enabled sources now")
    target = run.add_mutually_exclusive_group(required=True)
    target.add_argument("--source", help="Source id")
    target.add_argument("--all", action="store_true", help="All enabled sources")
    return parser


def _list_sources(settings: Settings) -> int:
    for source in load_sources(settings.config_dir).values():
        state = "enabled" if source.enabled else "disabled"
        print(f"{source.id}\t{source.type}\t{state}\t{source.schedule or 'manual'}")
    return 0


def _validate(settings: Settings) -> int:
    sources = load_sources(settings.config_dir)
    print(f"OK: {len(sources)} source definition(s) are valid")
    return 0


async def _migrate(settings: Settings) -> int:
    conn = await connect(settings)
    try:
        applied = await migrate(conn, settings.migrations_dir)
    finally:
        await conn.close()
    print(f"Applied: {', '.join(applied)}" if applied else "Schema is up to date")
    return 0


async def _run(settings: Settings, source_id: str | None) -> int:
    sources = load_sources(settings.config_dir)
    if source_id is not None and source_id not in sources:
        raise ConfigError(f"Unknown source: {source_id}")
    selected = [sources[source_id]] if source_id else [s for s in sources.values() if s.enabled]
    conn = await connect(settings)
    failed = 0
    try:
        await migrate(conn, settings.migrations_dir)
        async with httpx.AsyncClient(headers={"User-Agent": settings.user_agent}, follow_redirects=True) as client:
            for source in selected:
                result = await run_source(conn, source, settings, client)
                print(
                    f"{source.id}: {result.status} written={result.stats.written} "
                    f"skipped={result.stats.skipped} {result.error or ''}"
                )
                failed += result.status != "success"
    finally:
        await conn.close()
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("GIP_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parser().parse_args(argv)
    try:
        settings = Settings.from_env()
        if args.command == "list-sources":
            return _list_sources(settings)
        if args.command == "validate-config":
            return _validate(settings)
        if args.command == "migrate":
            return asyncio.run(_migrate(settings))
        if args.command == "run":
            return asyncio.run(_run(settings, None if args.all else args.source))
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
