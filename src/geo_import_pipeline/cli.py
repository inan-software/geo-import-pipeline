"""Command line interface."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from .config import ConfigError, Settings, load_sources
from .db import connect, migrate


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="geo-import-pipeline", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list-sources", help="Show the configured sources")
    commands.add_parser("validate-config", help="Validate all source definitions and exit")
    commands.add_parser("migrate", help="Create or update the schema pipeline_meta")
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
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
