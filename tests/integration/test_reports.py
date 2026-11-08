import asyncio
import json
from pathlib import Path

import httpx

from geo_import_pipeline.config import SourceConfig
from geo_import_pipeline.runner import execute_run

from .wfs_fake import FakeWfs, feature, square


def source(source_id="demo-areas", host="wfs.test", columns=None, **top):
    return SourceConfig.model_validate(
        {
            "id": source_id,
            "type": "wfs",
            "key": ["code"],
            "fields": {"code": {"path": "code", "required": True}, "label": {"path": "label"}},
            "wfs": {
                "url": f"http://{host}/ows",
                "typename": "app:area",
                "source_crs": "EPSG:25833",
                "page_size": 2,
                "page_delay_s": 0,
                "retries": 0,
                "backoff_s": 0,
            },
            "target": {
                "kind": "geometry",
                "table": "example.regions",
                "columns": columns or {"region_key": "code", "name": "label"},
                "geom_column": "geom",
                "geom_type": "MULTIPOLYGON",
            },
            **top,
        }
    )


def run(settings, handler, sources=None):
    report, path = asyncio.run(
        execute_run(settings, sources or [source()], transport=httpx.MockTransport(handler))
    )
    return report, Path(path)


def areas(*items):
    return FakeWfs([feature(key, name, square(370000 + int(key) * 1000)) for key, name in items])


def changes(report, index=0):
    return report["sources"][index]["changes"]


def test_first_import_is_marked_as_baseline(settings):
    report, _ = run(settings, areas(("1", "A"), ("2", "B")))
    assert changes(report) == {"new": 2, "changed": 0, "deleted": 0, "baseline": True, "geometries_retained": 0}


def test_changes_are_business_changes_not_technical_ones(settings):
    run(settings, areas(("1", "A"), ("2", "B"), ("3", "C")))
    report, _ = run(settings, areas(("1", "A"), ("2", "B-renamed"), ("4", "D")))
    assert changes(report) == {"new": 1, "changed": 1, "deleted": 1, "baseline": False, "geometries_retained": 1}
    samples = report["sources"][0]["change_samples"]
    assert samples == {"new": ["4"], "changed": ["2"], "deleted": ["3"]}


def test_identical_second_import_reports_no_changes(settings):
    handler = areas(("1", "A"), ("2", "B"))
    run(settings, handler)
    report, _ = run(settings, handler)
    assert changes(report)["new"] == changes(report)["changed"] == changes(report)["deleted"] == 0


def test_changed_geometry_counts_as_changed(settings):
    run(settings, FakeWfs([feature("1", "A", square(370000))]))
    report, _ = run(settings, FakeWfs([feature("1", "A", square(370050))]))
    assert changes(report)["changed"] == 1


def test_volatile_feature_ids_are_not_a_change(settings):
    run(settings, FakeWfs([feature("1", "A", square(370000), gml_id="run_one")]))
    report, _ = run(settings, FakeWfs([feature("1", "A", square(370000), gml_id="run_two")]))
    assert changes(report)["changed"] == 0


def test_report_files_exist_and_contain_the_required_content(settings):
    report, path = run(settings, areas(("1", "A")))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == report
    entry = data["sources"][0]
    assert entry["fetched_at"] and entry["duration_s"] is not None and data["duration_s"] >= 0
    assert path.with_suffix(".txt").exists()


def test_incomplete_fetch_deletes_nothing_and_counts_nothing(settings, db):
    run(settings, areas(("1", "A"), ("2", "B")))
    short = FakeWfs([feature("1", "A", square(370000))], hits=2)
    report, _ = run(settings, short)
    assert report["status"] == "failed" and changes(report)["deleted"] == 0
    state = db(lambda conn: conn.fetchval("SELECT count(*) FROM pipeline_meta.source_state"))
    assert state == 2


def test_empty_result_is_treated_as_incomplete(settings):
    run(settings, areas(("1", "A")))
    report, _ = run(settings, FakeWfs([]))
    assert report["status"] == "failed"
    assert "at least 1" in report["sources"][0]["error"]


def test_failing_source_does_not_stop_the_next_one(settings, db):
    good = areas(("1", "A"))

    def handler(request):
        return httpx.Response(500) if request.url.host == "broken.test" else good(request)

    report, _ = run(settings, handler, [source("broken", host="broken.test"), source("healthy")])
    assert report["status"] == "partial"
    assert [s["status"] for s in report["sources"]] == ["failed", "success"]
    assert report["failed_sources"][0]["source_id"] == "broken"
    assert "failed after" in report["failed_sources"][0]["error"]
    assert db(lambda conn: conn.fetchval("SELECT count(*) FROM example.regions")) == 1


def test_database_error_during_replace_rolls_back_everything(settings, db):
    good = source(columns={"region_key": "code", "name": "label"})
    run(settings, areas(("1", "A")), [good])
    # "label" cannot be cast to a date, so the replace fails inside its transaction.
    bad = source(columns={"region_key": "code", "updated_on": "label"})
    report, _ = run(settings, areas(("1", "A"), ("2", "B")), [bad])
    assert report["status"] == "failed"
    assert "InvalidDatetimeFormat" in report["sources"][0]["error"]

    async def facts(conn):
        return (
            await conn.fetchval("SELECT count(*) FROM example.regions"),
            await conn.fetchval("SELECT count(*) FROM pipeline_meta.source_state"),
        )

    assert db(facts) == (1, 1)


def test_no_staging_tables_remain_after_success_or_failure(settings, db):
    run(settings, areas(("1", "A")))
    run(settings, FakeWfs([feature("1", "A", square(370000))], hits=9))
    remaining = db(
        lambda conn: conn.fetchval("SELECT count(*) FROM pg_tables WHERE schemaname='pipeline_meta' AND tablename LIKE 'stg_%'")
    )
    assert remaining == 0


def test_runs_are_protocolled(settings, db):
    report, path = run(settings, areas(("1", "A")))

    async def facts(conn):
        return await conn.fetchrow(
            """SELECT r.status, r.report_path, s.new_rows, s.valid_rows
               FROM pipeline_meta.runs r JOIN pipeline_meta.source_runs s USING (run_id)
               WHERE r.run_id = $1::uuid""",
            report["run_id"],
        )

    row = db(facts)
    assert row["status"] == "success" and row["report_path"] == str(path)
    assert row["new_rows"] == 1 and row["valid_rows"] == 1


def test_report_is_written_when_the_database_is_unreachable(settings):
    import dataclasses

    unreachable = dataclasses.replace(settings, db_host="127.0.0.1", db_port=1)
    report, path = run(unreachable, areas(("1", "A")))
    assert report["status"] == "failed" and path.exists()
    assert "Database connection failed" in report["sources"][0]["error"]
