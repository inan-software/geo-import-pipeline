from geo_import_pipeline.config import SourceConfig
from geo_import_pipeline.model import RawRecord, WarningLog
from geo_import_pipeline.transform import Transformer


def make(fields, key=("k",), filters=(), decimal=",", null_values=("",)):
    cfg = SourceConfig.model_validate(
        {
            "id": "demo",
            "type": "csv",
            "key": list(key),
            "fields": fields,
            "filters": list(filters),
            "csv": {"path": "x.csv", "decimal": decimal, "null_values": list(null_values)},
            "target": {"kind": "attributes", "table": "s.t", "columns": {}, "source_column": "c", "source_value": "v"},
        }
    )
    log = WarningLog()
    return Transformer(cfg, log), log


def rec(**values):
    return RawRecord(values=values, ref="r1")


def test_leading_zeros_are_restored_for_numeric_looking_keys():
    transformer, _ = make({"k": {"path": "k", "pad": 8}})
    assert '"k": "01001000"' in transformer.apply(rec(k="1001000")).attrs


def test_decimal_comma_is_converted():
    transformer, _ = make({"k": {"path": "k"}, "v": {"path": "v", "type": "float"}})
    assert '"v": 1234.5' in transformer.apply(rec(k="a", v="1234,5")).attrs


def test_missing_key_skips_the_record():
    transformer, log = make({"k": {"path": "k"}})
    assert transformer.apply(rec(k="  ")) is None
    assert log.as_list()[0]["code"] == "missing_required_field"


def test_null_marker_counts_as_missing():
    transformer, log = make(
        {"k": {"path": "k"}, "v": {"path": "v", "type": "float", "required": True}}, null_values=("", ".")
    )
    assert transformer.apply(rec(k="a", v=".")) is None
    assert "v" in log.as_list()[0]["message"]


def test_unconvertible_optional_value_becomes_null_with_a_warning():
    transformer, log = make({"k": {"path": "k"}, "v": {"path": "v", "type": "integer"}})
    assert '"v": null' in transformer.apply(rec(k="a", v="abc")).attrs
    assert log.as_list()[0]["code"] == "conversion_failed"


def test_unconvertible_required_value_skips_the_record():
    transformer, _ = make({"k": {"path": "k"}, "v": {"path": "v", "type": "float", "required": True}})
    assert transformer.apply(rec(k="a", v="abc")) is None


def test_german_date_format():
    transformer, _ = make({"k": {"path": "k"}, "d": {"path": "d", "type": "date", "date_format": "%d.%m.%Y"}})
    assert '"d": "2024-12-31"' in transformer.apply(rec(k="a", d="31.12.2024")).attrs


def test_same_content_gives_same_hash_and_changed_value_differs():
    transformer, _ = make({"k": {"path": "k"}, "v": {"path": "v", "type": "float"}})
    first = transformer.apply(rec(k="a", v="1,5")).content_hash
    assert transformer.apply(rec(k="b", v="1,5")).content_hash == first
    assert transformer.apply(rec(k="a", v="2,5")).content_hash != first


def test_key_order_is_significant():
    transformer, _ = make({"a": {"path": "a"}, "b": {"path": "b"}}, key=("a", "b"))
    assert transformer.apply(rec(a="x", b="yz")).key_hash != transformer.apply(rec(a="xy", b="z")).key_hash


def test_prefix_filter_sees_the_padded_value():
    transformer, _ = make({"k": {"path": "k", "pad": 8}}, filters=[{"field": "k", "prefix": "01"}])
    assert transformer.matches(rec(k="1001000"))
    assert not transformer.matches(rec(k="12051000"))


def test_in_filter():
    transformer, _ = make({"k": {"path": "k"}}, filters=[{"field": "k", "in": ["a", "b"]}])
    assert transformer.matches(rec(k="a")) and not transformer.matches(rec(k="c"))
