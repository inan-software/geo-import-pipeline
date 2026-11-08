import httpx

from geo_import_pipeline.config import SourceConfig
from geo_import_pipeline.db import connect
from geo_import_pipeline.runner import run_source

from .wfs_fake import FakeWfs, bowtie, feature, square


def source(**wfs_changes):
    return SourceConfig.model_validate(
        {
            "id": "demo-areas",
            "type": "wfs",
            "key": ["code"],
            "fields": {"code": {"path": "code", "pad": 5, "required": True}, "label": {"path": "label"}},
            "wfs": {
                "url": "http://wfs.test/ows",
                "typename": "app:area",
                "source_crs": "EPSG:25833",
                "page_size": 2,
                "page_delay_s": 0,
                "retries": 1,
                "backoff_s": 0,
                **wfs_changes,
            },
            "target": {
                "kind": "geometry",
                "table": "example.regions",
                "columns": {"region_key": "code", "name": "label"},
                "geom_column": "geom",
                "geom_type": "MULTIPOLYGON",
            },
        }
    )


def run(settings, db, handler, cfg=None):
    async def work(conn):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await run_source(conn, cfg or source(), settings, client)

    return db(work)


def rows(db):
    async def work(conn):
        return await conn.fetch(
            "SELECT region_key, name, ST_SRID(geom) AS srid, GeometryType(geom) AS t, ST_IsValid(geom) AS valid "
            "FROM example.regions ORDER BY region_key"
        )

    return db(work)


def codes(result):
    return {w["code"] for w in result.stats.warnings.as_list()}


def test_all_pages_are_imported_with_padded_keys(settings, db):
    handler = FakeWfs([feature(str(i), f"N{i}", square(370000 + i * 1000)) for i in range(5)])
    result = run(settings, db, handler)
    assert result.status == "success", result.error
    found = rows(db)
    assert [r["region_key"] for r in found] == ["00000", "00001", "00002", "00003", "00004"]
    assert all(r["srid"] == 4326 and r["t"] == "MULTIPOLYGON" and r["valid"] for r in found)
    # One request for the count and three pages of at most two features.
    assert len(handler.requests) == 4


def test_second_run_updates_in_place(settings, db):
    run(settings, db, FakeWfs([feature("1", "Old", square(370000))]))
    run(settings, db, FakeWfs([feature("1", "New", square(370000))]))
    found = rows(db)
    assert len(found) == 1 and found[0]["name"] == "New"


def test_geometry_of_a_vanished_feature_stays(settings, db):
    run(settings, db, FakeWfs([feature("1", "A", square(370000)), feature("2", "B", square(371000))]))
    run(settings, db, FakeWfs([feature("1", "A", square(370000))]))
    assert [r["region_key"] for r in rows(db)] == ["00001", "00002"]


def test_invalid_geometry_is_repaired_with_a_warning(settings, db):
    result = run(settings, db, FakeWfs([feature("1", "A", bowtie(370000))]))
    assert result.status == "success", result.error
    assert rows(db)[0]["valid"] is True
    assert "geometry_repaired" in codes(result)


def test_unreadable_geometry_is_skipped_with_a_warning(settings, db):
    broken = feature("2", "B", "not numbers at all")
    result = run(settings, db, FakeWfs([feature("1", "A", square(370000)), broken]))
    assert result.status == "success", result.error
    assert [r["region_key"] for r in rows(db)] == ["00001"]
    assert result.stats.skipped == 1
    assert "geometry_parse_error" in codes(result)


def test_missing_key_is_skipped_with_a_warning(settings, db):
    no_key = feature("", "NoKey", square(371000))
    result = run(settings, db, FakeWfs([feature("1", "A", square(370000)), no_key]))
    assert result.status == "success", result.error
    assert len(rows(db)) == 1
    assert "missing_required_field" in codes(result)


def test_short_delivery_fails_and_changes_nothing(settings, db):
    run(settings, db, FakeWfs([feature("1", "A", square(370000))]))
    handler = FakeWfs([feature("1", "CHANGED", square(370000))], hits=5)
    result = run(settings, db, handler)
    assert result.status == "failed" and "Received 1" in result.error
    assert rows(db)[0]["name"] == "A"


def test_exception_report_fails_the_source(settings, db):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            text='<ows:ExceptionReport xmlns:ows="http://www.opengis.net/ows/1.1">'
            "<ows:Exception>boom</ows:Exception></ows:ExceptionReport>",
        )

    result = run(settings, db, handler)
    assert result.status == "failed" and "boom" in result.error
    assert len(calls) == 2  # One attempt plus one retry.


def test_transient_errors_are_retried(settings, db):
    inner = FakeWfs([feature("1", "A", square(370000))])
    failures = iter([500, 503])

    def handler(request):
        code = next(failures, None)
        return httpx.Response(code) if code else inner(request)

    result = run(settings, db, handler, source(retries=3))
    assert result.status == "success", result.error


def test_duplicate_keys_fail_a_wfs_source_by_default(settings, db):
    handler = FakeWfs([feature("1", "A", square(370000)), feature("1", "B", square(371000), gml_id="other")])
    result = run(settings, db, handler)
    assert result.status == "failed" and "duplicate" in result.error
    assert rows(db) == []


def test_target_structure_is_never_changed(settings, db):
    async def snapshot(conn):
        return await conn.fetch(
            """SELECT table_name, column_name, data_type, is_nullable, column_default
               FROM information_schema.columns WHERE table_schema = 'example' ORDER BY 1, 2"""
        )

    before = db(snapshot)
    run(settings, db, FakeWfs([feature("1", "A", square(370000))]))
    assert db(snapshot) == before


def test_missing_target_column_is_reported_before_fetching(settings, db):
    cfg = source()
    cfg.target.columns["no_such_column"] = "label"
    handler = FakeWfs([feature("1", "A", square(370000))])
    result = run(settings, db, handler, cfg)
    assert result.status == "failed" and "no_such_column" in result.error
    assert handler.requests == []  # Nothing is requested from the service for a broken setup.


def test_concurrent_run_of_the_same_source_is_refused(settings, db):
    async def work(conn):
        await conn.execute("SELECT pg_advisory_lock(hashtext('demo-areas'))")
        other = await connect(settings)
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(FakeWfs([]))) as client:
                return await run_source(other, source(), settings, client)
        finally:
            await other.close()

    result = db(work)
    assert result.status == "failed" and "Another run" in result.error


def test_wfs_110_fetches_everything_in_one_request_by_default(settings, db):
    calls, result = _run_110(settings, db, source(version="1.1.0"))
    assert result.status == "success", result.error
    assert "TYPENAME" in calls[0] and "TYPENAMES" not in calls[0]
    assert len(calls) == 2 and "MAXFEATURES" not in calls[1] and "STARTINDEX" not in calls[1]
    assert rows(db)[0]["srid"] == 4326


def test_wfs_110_can_page_when_the_service_supports_it(settings, db):
    calls, result = _run_110(settings, db, source(version="1.1.0", paging=True))
    assert result.status == "success", result.error
    assert "MAXFEATURES" in calls[1] and "STARTINDEX" in calls[1]


def _run_110(settings, db, cfg):
    calls = []

    def handler(request):
        params = dict(request.url.params)
        calls.append(params)
        if params.get("RESULTTYPE") == "hits":
            return httpx.Response(
                200, text='<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs" numberOfFeatures="1"/>'
            )
        body = (
            '<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs" xmlns:gml="http://www.opengis.net/gml" '
            'xmlns:app="http://example.org/app"><gml:featureMember><app:area gml:id="f1"><app:code>1</app:code>'
            '<app:label>A</app:label><app:geom><gml:MultiPolygon srsName="EPSG:25833"><gml:polygonMember><gml:Polygon>'
            f"<gml:exterior><gml:LinearRing><gml:posList>{square(370000)}</gml:posList></gml:LinearRing></gml:exterior>"
            "</gml:Polygon></gml:polygonMember></gml:MultiPolygon></app:geom></app:area></gml:featureMember>"
            "</wfs:FeatureCollection>"
        )
        return httpx.Response(200, text=body)

    return calls, run(settings, db, handler, cfg)


def test_service_ignoring_the_start_index_is_detected(settings, db):
    """Observed on a real WFS 1.1.0: every page repeats the first features."""
    fake = FakeWfs([feature(str(i), f"N{i}", square(370000 + i * 1000)) for i in range(3)])

    def ignores_start(request):
        return fake(httpx.Request("GET", request.url.copy_set_param("STARTINDEX", "0")))

    result = run(settings, db, ignores_start)
    assert result.status == "failed"
    assert rows(db) == []
