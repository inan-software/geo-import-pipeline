"""The adapter on its own, without a database."""

import asyncio
import zipfile
from pathlib import Path

import httpx
import pytest

from geo_import_pipeline.config import SourceConfig
from geo_import_pipeline.model import IncompleteFetchError, SourceStats
from geo_import_pipeline.sources import adapter_for


def adapter(path, tmp_path, **csv_settings):
    cfg = SourceConfig.model_validate(
        {
            "id": "demo",
            "type": "csv",
            "key": ["a"],
            "fields": {"a": {"path": "a"}},
            "csv": {"path": str(path), **csv_settings},
            "target": {"kind": "attributes", "table": "s.t", "columns": {}, "source_column": "c", "source_value": "v"},
        }
    )
    return adapter_for(cfg, httpx.AsyncClient(), SourceStats(), Path(tmp_path))


def big_zip(tmp_path, name="data.csv"):
    path = tmp_path / "big.zip"
    # Far more than a pipe buffer holds, so unzip is still writing when the reader stops.
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, "a;b\n" + "x;y\n" * 500_000)
    return path


def test_stopping_early_does_not_hang_on_the_unzip_process(tmp_path):
    source_adapter = adapter(big_zip(tmp_path), tmp_path, archive="zip", member="data*.csv")

    async def work():
        records = source_adapter.records()
        first = await records.__anext__()
        await asyncio.wait_for(records.aclose(), timeout=20)
        return first

    assert asyncio.run(work()).values["a"] == "x"


def test_archive_without_a_matching_member_is_incomplete(tmp_path):
    source_adapter = adapter(big_zip(tmp_path), tmp_path, archive="zip", member="other*.csv")

    async def work():
        async for _ in source_adapter.records():
            pass

    with pytest.raises(IncompleteFetchError, match="exactly one"):
        asyncio.run(work())
