import asyncio
import zipfile
from datetime import datetime, timezone
from io import BytesIO

import httpx

from geo_import_pipeline.config import SourceConfig
from geo_import_pipeline.runner import execute_run

HEADER = "Bereich;Kuerzel;Raumbezug;Kennziffer;Zeitbezug;Wert\n"
ROWS = (
    "A;x1;Gemeinden;12000001;2020;1,5\n"
    "A;x1;Gemeinden;12000001;2021;2,5\n"
    "B;x1;Gemeinden;12000001;2021;9,5\n"
    "A;x1;Kreise;12000000;2021;7,0\n"
    "A;x1;Gemeinden;01000001;2021;3,5\n"
)
THIS_YEAR = datetime.now(timezone.utc).year


def source(csv_settings, source_id="demo-csv", source_value="DEMO", filters=None, **top):
    return SourceConfig.model_validate(
        {
            "id": source_id,
            "type": "csv",
            "key": ["code", "area", "short", "period"],
            "fields": {
                "code": {"path": "Kennziffer", "pad": 8},
                "area": {"path": "Bereich"},
                "short": {"path": "Kuerzel"},
                "level": {"path": "Raumbezug"},
                "period": {"path": "Zeitbezug"},
                "value": {"path": "Wert", "type": "float", "required": True},
            },
            "filters": filters
            if filters is not None
            else [{"field": "level", "equals": "Gemeinden"}, {"field": "code", "prefix": "12"}],
            "csv": {"decimal": ",", "retries": 0, "backoff_s": 0, **csv_settings},
            "target": {
                "kind": "attributes",
                "table": "example.indicator_values",
                "source_column": "source",
                "source_value": source_value,
                "columns": {"region_key": "code", "indicator": "short", "period": "period", "value": "value", "level": "level"},
                "metadata": {
                    "table": "example.dataset_metadata",
                    "key": {"dataset": source_id},
                    "values": {"publisher": source_value, "title": "T {version}"},
                },
            },
            **top,
        }
    )


def zip_bytes(content, name="data_x.csv", method=zipfile.ZIP_DEFLATED):
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", method) as archive:
        archive.writestr("readme.txt", "not the data")
        archive.writestr(name, content)
    return buffer.getvalue()


class FakeDownloads:
    def __init__(self, files, etag='"v1"'):
        self.files = files
        self.etag = etag
        self.gets = []
        self.heads = []

    def __call__(self, request):
        name = request.url.path.rsplit("/", 1)[-1]
        if name not in self.files:
            return httpx.Response(404)
        body = self.files[name]
        headers = {"etag": self.etag, "last-modified": "Mon, 01 Jan 2024 00:00:00 GMT", "content-length": str(len(body))}
        if request.method == "HEAD":
            self.heads.append(name)
            return httpx.Response(200, headers=headers)
        self.gets.append(name)
        return httpx.Response(200, headers=headers, content=body)


def run(settings, cfg, handler=None):
    transport = httpx.MockTransport(handler or (lambda r: httpx.Response(500)))
    report, _ = asyncio.run(execute_run(settings, [cfg], transport=transport))
    return report


def values(db):
    async def work(conn):
        return await conn.fetch(
            "SELECT region_key, indicator, period, value, source FROM example.indicator_values ORDER BY period, value"
        )

    return db(work)


def write_csv(tmp_path, text, name="data.csv", encoding="utf-8"):
    path = tmp_path / name
    path.write_text(text, encoding=encoding)
    return str(path)


def test_local_file_is_filtered_mapped_and_converted(settings, db, tmp_path):
    report = run(settings, source({"path": write_csv(tmp_path, HEADER + ROWS)}))
    assert report["status"] == "success", report["failed_sources"]
    rows = report["sources"][0]["rows"]
    assert (rows["read"], rows["filtered_out"], rows["valid"], rows["written"]) == (5, 2, 3, 3)
    found = values(db)
    assert [(r["period"], r["value"]) for r in found] == [("2020", 1.5), ("2021", 2.5), ("2021", 9.5)]
    assert found[0]["source"] == "DEMO"


def test_key_parts_missing_in_the_target_still_diff_correctly(settings, tmp_path):
    # Two rows differ only in Bereich, which is not a target column.
    report = run(settings, source({"path": write_csv(tmp_path, HEADER + ROWS)}))
    assert report["sources"][0]["changes"]["new"] == 3
    changed = ROWS.replace("B;x1;Gemeinden;12000001;2021;9,5", "B;x1;Gemeinden;12000001;2021;9,6")
    report = run(settings, source({"path": write_csv(tmp_path, HEADER + changed)}))
    assert report["sources"][0]["changes"] == {"new": 0, "changed": 1, "deleted": 0, "baseline": False}


def test_rerun_replaces_only_rows_of_the_same_source(settings, db, tmp_path):
    first = write_csv(tmp_path, HEADER + "A;x1;Gemeinden;12000001;2020;1,5\n", "a.csv")
    other = write_csv(tmp_path, HEADER + "A;y1;Gemeinden;12000002;2020;4,0\n", "b.csv")
    run(settings, source({"path": first}))
    run(settings, source({"path": other}, source_id="other-csv", source_value="OTHER"))
    report = run(settings, source({"path": write_csv(tmp_path, HEADER + "A;x1;Gemeinden;12000001;2021;2,0\n", "a.csv")}))
    assert report["sources"][0]["changes"] == {"new": 1, "changed": 0, "deleted": 1, "baseline": False}
    assert sorted((r["source"], r["period"]) for r in values(db)) == [("DEMO", "2021"), ("OTHER", "2020")]


def test_newest_year_is_found_with_head_requests(settings, tmp_path):
    name = f"data_{THIS_YEAR - 1}.zip"
    handler = FakeDownloads({name: zip_bytes(HEADER + ROWS)})
    cfg = source({"url_template": "http://files.test/data_{year}.zip", "archive": "zip", "member": "data*.csv"})
    report = run(settings, cfg, handler)
    assert report["status"] == "success", report["failed_sources"]
    assert name in report["sources"][0]["source_version"]
    assert handler.gets == [name]


def test_no_release_found_fails_the_source(settings):
    cfg = source({"url_template": "http://files.test/data_{year}.zip", "archive": "zip", "member": "*.csv", "year_lookback": 1})
    report = run(settings, cfg, FakeDownloads({}))
    assert report["status"] == "failed" and "No file found" in report["sources"][0]["error"]


def test_unchanged_archive_is_not_downloaded_again(settings):
    handler = FakeDownloads({"d.zip": zip_bytes(HEADER + ROWS)})
    cfg = source({"url": "http://files.test/d.zip", "archive": "zip", "member": "data*.csv"})
    run(settings, cfg, handler)
    report = run(settings, cfg, handler)
    assert handler.gets == ["d.zip"]
    assert report["sources"][0]["changes"]["new"] == 0


def test_changed_archive_is_downloaded_and_replaces_the_cache(settings):
    handler = FakeDownloads({"d.zip": zip_bytes(HEADER + ROWS)})
    cfg = source({"url": "http://files.test/d.zip", "archive": "zip", "member": "data*.csv"})
    run(settings, cfg, handler)
    handler.files["d.zip"] = zip_bytes(HEADER + ROWS.replace("1,5", "1,7"))
    handler.etag = '"v2"'
    report = run(settings, cfg, handler)
    assert handler.gets == ["d.zip", "d.zip"]
    assert report["sources"][0]["changes"]["changed"] == 1
    assert sorted(p.name for p in (settings.cache_dir / "demo-csv").iterdir()) == ["d.zip", "d.zip.meta.json"]


def test_damaged_archive_fails_and_keeps_existing_data(settings, db):
    good = zip_bytes(HEADER + ROWS)
    handler = FakeDownloads({"d.zip": good})
    cfg = source({"url": "http://files.test/d.zip", "archive": "zip", "member": "data*.csv"})
    run(settings, cfg, handler)

    payload = (HEADER + ROWS * 2000).encode("utf-8")
    damaged = bytearray(zip_bytes(payload, method=zipfile.ZIP_STORED))
    damaged[damaged.index(b"Kreise", 5000)] = ord("X")  # Same length, wrong checksum.
    handler.files["d.zip"] = bytes(damaged)
    handler.etag = '"v2"'
    report = run(settings, cfg, handler)
    assert report["status"] == "failed" and "damaged" in report["sources"][0]["error"]
    assert len(values(db)) == 3


def test_truncated_download_fails_and_keeps_existing_data(settings, db, tmp_path):
    run(settings, source({"path": write_csv(tmp_path, HEADER + ROWS)}))

    def handler(request):
        headers = {"etag": '"x"', "content-length": "99999"}
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers)
        return httpx.Response(200, headers=headers, content=b"short")

    report = run(settings, source({"url": "http://files.test/d.csv"}), handler)
    assert report["status"] == "failed" and "Received 5 of 99999" in report["sources"][0]["error"]
    assert len(values(db)) == 3
    assert not (settings.cache_dir / "demo-csv" / "d.csv").exists()


def test_missing_column_fails_and_names_the_column(settings, db, tmp_path):
    run(settings, source({"path": write_csv(tmp_path, HEADER + ROWS)}))
    report = run(settings, source({"path": write_csv(tmp_path, "Bereich;Kuerzel;Raumbezug;Kennziffer;Zeitbezug\nA;b;c;d;e\n")}))
    assert report["status"] == "failed" and "Wert" in report["sources"][0]["error"]
    assert len(values(db)) == 3


def test_missing_value_is_skipped_with_a_warning(settings, db, tmp_path):
    text = HEADER + "A;x1;Gemeinden;12000001;2020;\nA;x1;Gemeinden;12000001;2021;2,5\n"
    report = run(settings, source({"path": write_csv(tmp_path, text)}))
    entry = report["sources"][0]
    assert entry["rows"]["skipped"] == 1 and len(values(db)) == 1
    assert entry["warnings"][0]["code"] == "missing_required_field"
    assert entry["warnings"][0]["samples"] == ["line 2"]


def test_malformed_row_is_skipped_with_a_warning(settings, db, tmp_path):
    report = run(settings, source({"path": write_csv(tmp_path, HEADER + "only;three;cols\nA;x1;Gemeinden;12000001;2021;2,5\n")}))
    entry = report["sources"][0]
    assert entry["status"] == "success" and entry["warnings"][0]["code"] == "malformed_row"
    assert len(values(db)) == 1


def test_wrong_encoding_fails_the_source(settings, tmp_path):
    report = run(settings, source({"path": write_csv(tmp_path, HEADER + "Ä;x;Gemeinden;12000001;2021;2,5\n", encoding="latin-1")}))
    assert report["status"] == "failed" and "not valid utf-8" in report["sources"][0]["error"]


def test_filter_without_matches_deletes_nothing(settings, db, tmp_path):
    path = write_csv(tmp_path, HEADER + ROWS)
    run(settings, source({"path": path}))
    report = run(settings, source({"path": path}, filters=[{"field": "level", "equals": "Nowhere"}]))
    assert report["status"] == "failed" and len(values(db)) == 3


def test_duplicate_keys_keep_the_first_row_with_a_warning(settings, db, tmp_path):
    text = HEADER + "A;x1;Gemeinden;12000001;2021;2,5\nA;x1;Gemeinden;12000001;2021;8,5\n"
    report = run(settings, source({"path": write_csv(tmp_path, text)}))
    assert report["sources"][0]["warnings"][0]["code"] == "duplicate_key"
    assert [r["value"] for r in values(db)] == [2.5]


def test_metadata_row_is_created_and_updated(settings, db, tmp_path):
    path = write_csv(tmp_path, HEADER + ROWS)
    run(settings, source({"path": path}))
    run(settings, source({"path": path}))

    async def facts(conn):
        return await conn.fetch("SELECT publisher, title, modified FROM example.dataset_metadata")

    found = db(facts)
    assert len(found) == 1 and found[0]["publisher"] == "DEMO"
    assert found[0]["title"].startswith("T data.csv") and found[0]["modified"] is not None


def test_custom_adapter_can_be_registered(settings, db):
    from geo_import_pipeline.model import RawRecord
    from geo_import_pipeline.sources import SourceAdapter, register

    @register("custom-test")
    class Custom(SourceAdapter):
        async def records(self):
            yield RawRecord(values={"Kennziffer": "12000001", "Bereich": "A", "Kuerzel": "c", "Raumbezug": "Gemeinden", "Zeitbezug": "2021", "Wert": "1.5"})

    data = source({"path": "unused"}).model_dump(by_alias=True)
    data.update(type="custom-test", csv=None)
    report = run(settings, SourceConfig.model_validate(data))
    assert report["status"] == "success", report["failed_sources"]
    assert [r["value"] for r in values(db)] == [1.5]
