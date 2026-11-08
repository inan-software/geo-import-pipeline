import pytest

from geo_import_pipeline.config import ConfigError
from geo_import_pipeline.sources import base, load_plugins


def test_plugin_module_registers_its_adapter(tmp_path, monkeypatch):
    (tmp_path / "gip_test_plugin.py").write_text(
        "from geo_import_pipeline.sources import SourceAdapter, register\n"
        "@register('from-plugin')\n"
        "class Plugin(SourceAdapter):\n"
        "    async def records(self):\n"
        "        return\n"
        "        yield\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    load_plugins(["gip_test_plugin"])
    assert "from-plugin" in base._REGISTRY


def test_missing_plugin_is_a_configuration_error():
    with pytest.raises(ConfigError, match="no_such_module_xyz"):
        load_plugins(["no_such_module_xyz"])
