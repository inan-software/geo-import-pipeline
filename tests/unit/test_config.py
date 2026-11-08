from pathlib import Path

import pytest

from geo_import_pipeline.config import ConfigError, SourceConfig, load_source_file, load_sources

BASE = {
    "id": "demo",
    "type": "wfs",
    "key": ["k"],
    "fields": {"k": {"path": "k"}},
    "wfs": {"url": "http://x/", "typename": "a:b", "source_crs": "EPSG:25833"},
    "target": {
        "kind": "geometry",
        "table": "s.t",
        "columns": {"k": "k"},
        "geom_column": "geom",
        "geom_type": "MULTIPOLYGON",
    },
}


def variant(**changes):
    data = {**BASE, **changes}
    return SourceConfig.model_validate(data)


def test_valid_source_is_accepted():
    assert variant().on_duplicate == "fail"


def test_shipped_configurations_are_valid():
    sources = load_sources(Path(__file__).parents[2] / "config")
    assert "geobasis-bb-gemeinden" in sources


@pytest.mark.parametrize(
    "change",
    [
        {"key": ["missing"]},
        {"schedule": "not a cron"},
        {"id": "Has Spaces"},
        {"unknown_option": 1},
        {"filters": [{"field": "missing", "equals": "x"}]},
    ],
)
def test_invalid_source_is_rejected(change):
    with pytest.raises(ValueError):
        variant(**change)


def test_geometry_can_never_be_configured_for_deletion():
    target = {**BASE["target"], "delete_missing": True}
    with pytest.raises(ValueError):
        variant(target=target)


def test_key_fields_must_reach_the_target():
    target = {**BASE["target"], "columns": {}}
    with pytest.raises(ValueError):
        variant(target=target)


def test_target_may_not_live_in_control_schema():
    target = {**BASE["target"], "table": "pipeline_meta.x"}
    with pytest.raises(ValueError):
        variant(target=target)


@pytest.mark.parametrize("table", ["no_schema", "a.b.c", 'a."b;drop'])
def test_table_names_must_be_plain_identifiers(table):
    target = {**BASE["target"], "table": table}
    with pytest.raises(ValueError):
        variant(target=target)


def test_environment_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("WFS_URL", "http://from-env/")
    path = tmp_path / "s.yaml"
    path.write_text(
        """
id: demo
type: wfs
key: [k]
fields: {k: {path: k}}
wfs: {url: "${WFS_URL}", typename: "a:b", source_crs: "EPSG:25833"}
target: {kind: geometry, table: s.t, columns: {k: k}, geom_column: g, geom_type: POINT}
""",
        encoding="utf-8",
    )
    assert load_source_file(path).wfs.url == "http://from-env/"


def test_missing_environment_variable_is_a_clear_error(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text("id: ${DOES_NOT_EXIST_XYZ}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="DOES_NOT_EXIST_XYZ"):
        load_source_file(path)
