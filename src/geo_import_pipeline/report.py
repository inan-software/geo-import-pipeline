"""The import report: one JSON file per run and a readable summary next to it."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .runner_types import SourceResult

REPORT_VERSION = 1


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _source_entry(result: SourceResult) -> dict:
    diff = result.diff
    entry = {
        "source_id": result.source_id,
        "type": result.source_type,
        "status": result.status,
        "error": result.error,
        "started_at": _iso(result.started_at),
        "finished_at": _iso(result.finished_at),
        "fetched_at": _iso(result.fetched_at),
        "duration_s": result.duration_s,
        "source_version": result.version,
        "rows": {
            "read": result.stats.read,
            "filtered_out": result.stats.filtered_out,
            "valid": result.valid,
            "skipped": result.stats.skipped,
            "written": result.stats.written,
        },
        "changes": {
            "new": diff.new if diff else 0,
            "changed": diff.changed if diff else 0,
            "deleted": diff.deleted if diff else 0,
            "baseline": diff.baseline if diff else False,
        },
        "change_samples": diff.samples if diff else {},
        "warnings": result.stats.warnings.as_list(),
        "warning_count": result.stats.warnings.total,
    }
    if result.target_kind == "geometry":
        # Geometries are shared across sources, so a vanished key stays in the target.
        entry["changes"]["geometries_retained"] = diff.deleted if diff else 0
    return entry


def build_report(
    run_id: str, trigger: str, started_at: datetime, finished_at: datetime, results: Iterable[SourceResult]
) -> dict:
    results = list(results)
    failed = sum(r.status != "success" for r in results)
    status = "success" if not failed else ("failed" if failed == len(results) else "partial")
    return {
        "report_version": REPORT_VERSION,
        "run_id": run_id,
        "trigger": trigger,
        "status": status,
        "started_at": _iso(started_at),
        "finished_at": _iso(finished_at),
        "duration_s": round((finished_at - started_at).total_seconds(), 3),
        "sources": [_source_entry(r) for r in results],
        "failed_sources": [{"source_id": r.source_id, "error": r.error} for r in results if r.status != "success"],
    }


def render_summary(report: dict) -> str:
    lines = [
        f"Import run {report['run_id']}",
        f"Trigger: {report['trigger']}   Status: {report['status']}   Duration: {report['duration_s']} s",
        f"Started: {report['started_at']}",
        "",
    ]
    for source in report["sources"]:
        rows, changes = source["rows"], source["changes"]
        lines.append(f"{source['source_id']} ({source['type']}): {source['status']}, {source['duration_s']} s")
        if source["error"]:
            lines.append(f"  Error: {source['error']}")
        if source["fetched_at"]:
            lines.append(f"  Fetched: {source['fetched_at']}   Version: {source['source_version'] or '-'}")
        lines.append(
            f"  Rows: read {rows['read']}, filtered out {rows['filtered_out']}, valid {rows['valid']}, "
            f"skipped {rows['skipped']}, written {rows['written']}"
        )
        baseline = " (first import of this source)" if changes["baseline"] else ""
        lines.append(f"  Changes: new {changes['new']}, changed {changes['changed']}, deleted {changes['deleted']}{baseline}")
        if changes.get("geometries_retained"):
            lines.append(f"  Geometries kept although no longer delivered: {changes['geometries_retained']}")
        for warning in source["warnings"]:
            lines.append(f"  Warning [{warning['code']}] x{warning['count']}: {warning['message']}")
        lines.append("")
    return "\n".join(lines)


def _write_atomic(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def write_report(report_dir: Path, report: dict) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = report["started_at"].split(".")[0].split("+")[0].replace("-", "").replace(":", "")
    base = report_dir / f"import_{stamp}Z_{report['run_id'][:8]}"
    json_path = base.with_suffix(".json")
    _write_atomic(json_path, json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    _write_atomic(base.with_suffix(".txt"), render_summary(report) + "\n")
    return json_path
