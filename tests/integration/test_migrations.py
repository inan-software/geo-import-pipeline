from geo_import_pipeline.db import migrate

SQUARE = (
    '<gml:Polygon xmlns:gml="http://www.opengis.net/gml/3.2" srsName="urn:ogc:def:crs:EPSG::25833">'
    "<gml:exterior><gml:LinearRing><gml:posList>{}</gml:posList></gml:LinearRing></gml:exterior></gml:Polygon>"
)
VALID = SQUARE.format("370000 5800000 370100 5800000 370100 5800100 370000 5800100 370000 5800000")
BOWTIE = SQUARE.format("370000 5800000 370100 5800100 370100 5800000 370000 5800100 370000 5800000")


def test_migration_is_idempotent(settings, db):
    async def work(conn):
        return await migrate(conn, settings.migrations_dir)

    assert db(work) == []


def test_control_tables_exist(db):
    async def work(conn):
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'pipeline_meta'"
        )
        return {row["table_name"] for row in rows}

    assert {"runs", "source_runs", "source_state", "schema_migrations"} <= db(work)


async def _process(conn, gml, target="MULTIPOLYGON"):
    return await conn.fetchrow(
        "SELECT out_status, ST_SRID(out_geom) AS srid, GeometryType(out_geom) AS gtype, ST_IsValid(out_geom) AS valid, "
        "ST_XMin(out_geom) AS xmin, out_hash FROM pipeline_meta.process_gml($1, 25833, $2, 4326)",
        gml,
        target,
    )


def test_valid_polygon_is_reprojected_and_made_multi(db):
    row = db(lambda conn: _process(conn, VALID))
    assert row["out_status"] == "ok"
    assert row["srid"] == 4326 and row["gtype"] == "MULTIPOLYGON"
    assert 12 < row["xmin"] < 15  # Longitude, so the axes were not swapped.
    assert len(row["out_hash"]) == 32


def test_invalid_polygon_is_repaired(db):
    row = db(lambda conn: _process(conn, BOWTIE))
    assert row["out_status"] == "repaired" and row["valid"] is True


def test_broken_gml_reports_status_instead_of_raising(db):
    assert db(lambda conn: _process(conn, "<gml:nonsense/>"))["out_status"] == "parse_error"


def test_wrong_geometry_family_is_rejected(db):
    assert db(lambda conn: _process(conn, VALID, "POINT"))["out_status"] == "type_mismatch"
