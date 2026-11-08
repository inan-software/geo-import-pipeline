"""Loading and validation of the runtime settings and the source definitions."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
IDENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
GEOM_TYPES = ("POINT", "LINESTRING", "POLYGON", "MULTIPOINT", "MULTILINESTRING", "MULTIPOLYGON")


class ConfigError(Exception):
    """A configuration file or environment variable is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    """Runtime settings. Everything comes from the environment, nothing is hard-coded."""

    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    config_dir: Path
    report_dir: Path
    cache_dir: Path
    migrations_dir: Path
    user_agent: str
    timezone: str
    plugin_modules: tuple[str, ...]

    @classmethod
    def from_env(cls) -> Settings:
        password = os.environ.get("POSTGRES_PASSWORD")
        if not password:
            raise ConfigError("Environment variable POSTGRES_PASSWORD is not set")
        return cls(
            db_host=os.environ.get("POSTGRES_HOST", "db"),
            db_port=int(os.environ.get("POSTGRES_PORT", "5432")),
            db_name=os.environ.get("POSTGRES_DB", "geodata"),
            db_user=os.environ.get("POSTGRES_USER", "geodata"),
            db_password=password,
            config_dir=Path(os.environ.get("GIP_CONFIG_DIR", "/app/config")),
            report_dir=Path(os.environ.get("GIP_REPORT_DIR", "/data/reports")),
            cache_dir=Path(os.environ.get("GIP_CACHE_DIR", "/data/cache")),
            migrations_dir=Path(os.environ.get("GIP_MIGRATIONS_DIR", "/app/migrations")),
            user_agent=os.environ.get("GIP_USER_AGENT", "geo-import-pipeline/0.1"),
            timezone=os.environ.get("GIP_TIMEZONE", "UTC"),
            plugin_modules=tuple(
                m.strip() for m in os.environ.get("GIP_PLUGIN_MODULES", "").split(",") if m.strip()
            ),
        )


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class FieldConfig(_Model):
    """One mapped field: either read from the source (``path``) or a constant."""

    path: str | None = None
    const: str | int | float | None = None
    type: Literal["text", "integer", "float", "date"] = "text"
    pad: int | None = Field(default=None, ge=1)
    date_format: str | None = None
    required: bool = False

    @model_validator(mode="after")
    def _exactly_one_origin(self) -> FieldConfig:
        if (self.path is None) == (self.const is None):
            raise ValueError("a field needs exactly one of 'path' or 'const'")
        return self


class FilterConfig(_Model):
    """Keep only records whose field matches. Exactly one condition per filter."""

    field: str
    equals: str | None = None
    in_: list[str] | None = Field(default=None, alias="in")
    prefix: str | None = None

    @model_validator(mode="after")
    def _exactly_one_condition(self) -> FilterConfig:
        if sum(v is not None for v in (self.equals, self.in_, self.prefix)) != 1:
            raise ValueError("a filter needs exactly one of 'equals', 'in' or 'prefix'")
        return self


class HttpConfig(_Model):
    timeout_s: float = Field(default=120.0, gt=0)
    retries: int = Field(default=4, ge=0, le=10)
    backoff_s: float = Field(default=2.0, ge=0)


class WfsConfig(HttpConfig):
    url: str
    version: Literal["2.0.0", "1.1.0"] = "2.0.0"
    typename: str
    page_size: int = Field(default=500, ge=1)
    page_delay_s: float = Field(default=1.0, ge=0)
    source_crs: str
    geometry_path: str = "geom"
    sort_by: str | None = None
    output_format: str | None = None
    paging: bool | None = None

    @property
    def paged(self) -> bool:
        """WFS 1.1.0 has no paging in the standard, so it is off there unless requested."""
        return self.version == "2.0.0" if self.paging is None else self.paging

    @property
    def fallback_srid(self) -> int:
        match = re.search(r"(\d+)\s*$", self.source_crs)
        if not match:
            raise ConfigError(f"Cannot read an EPSG code from source_crs '{self.source_crs}'")
        return int(match.group(1))


class CsvConfig(HttpConfig):
    url: str | None = None
    url_template: str | None = None
    year_lookback: int = Field(default=3, ge=0, le=20)
    path: str | None = None
    archive: Literal["none", "zip"] = "none"
    member: str | None = None
    delimiter: str = Field(default=";", min_length=1, max_length=1)
    quotechar: str = Field(default='"', min_length=1, max_length=1)
    decimal: str = Field(default=".", min_length=1, max_length=1)
    thousands: str | None = None
    encoding: str = "utf-8"
    null_values: list[str] = Field(default_factory=lambda: [""])

    @model_validator(mode="after")
    def _exactly_one_location(self) -> CsvConfig:
        if sum(v is not None for v in (self.url, self.url_template, self.path)) != 1:
            raise ValueError("csv needs exactly one of 'url', 'url_template' or 'path'")
        if self.url_template is not None and "{year}" not in self.url_template:
            raise ValueError("'url_template' must contain {year}")
        if self.archive == "zip" and not self.member:
            raise ValueError("'member' is required for archive: zip")
        return self


class MetadataConfig(_Model):
    """One row describing the dataset, written in the same transaction as the data."""

    table: str
    key: dict[str, str]
    values: dict[str, str] = Field(default_factory=dict)

    @field_validator("key")
    @classmethod
    def _key_not_empty(cls, value: dict[str, str]) -> dict[str, str]:
        if not value:
            raise ValueError("metadata.key must not be empty")
        return value


class TargetConfig(_Model):
    kind: Literal["geometry", "attributes"]
    table: str
    columns: dict[str, str]  # target column -> field name
    geom_column: str | None = None
    geom_type: Literal[*GEOM_TYPES] | None = None  # type: ignore[valid-type]
    srid: int = 4326
    # Geometries are shared across sources and are never removed.
    delete_missing: Literal[False] = False
    source_column: str | None = None
    source_value: str | None = None
    metadata: MetadataConfig | None = None

    @model_validator(mode="after")
    def _kind_requirements(self) -> TargetConfig:
        if self.kind == "geometry" and not (self.geom_column and self.geom_type):
            raise ValueError("geometry targets need 'geom_column' and 'geom_type'")
        if self.kind == "attributes" and not (self.source_column and self.source_value):
            raise ValueError("attribute targets need 'source_column' and 'source_value'")
        if (self.source_column is None) != (self.source_value is None):
            raise ValueError("'source_column' and 'source_value' belong together")
        return self

    @field_validator("table")
    @classmethod
    def _table_outside_control_schema(cls, value: str) -> str:
        split_table(value)
        if value.split(".")[0] == "pipeline_meta":
            raise ValueError("target tables must not live in schema pipeline_meta")
        return value


class SourceConfig(_Model):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    type: str
    enabled: bool = True
    schedule: str | None = None
    key: list[str] = Field(min_length=1)
    fields: dict[str, FieldConfig]
    filters: list[FilterConfig] = Field(default_factory=list)
    min_rows: int = Field(default=1, ge=0)
    duplicate_keys: Literal["warn", "fail"] | None = None
    wfs: WfsConfig | None = None
    csv: CsvConfig | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    target: TargetConfig

    @field_validator("schedule")
    @classmethod
    def _valid_cron(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                CronTrigger.from_crontab(value)
            except ValueError as exc:
                raise ValueError(f"invalid cron expression '{value}': {exc}") from exc
        return value

    @model_validator(mode="after")
    def _references_exist(self) -> SourceConfig:
        if self.type == "wfs" and self.wfs is None:
            raise ValueError("type wfs needs a 'wfs' section")
        if self.type == "csv" and self.csv is None:
            raise ValueError("type csv needs a 'csv' section")
        if self.target.kind == "geometry" and self.type == "csv":
            raise ValueError("csv sources carry no geometry; use target kind 'attributes'")
        for name in self.key:
            if name not in self.fields:
                raise ValueError(f"key field '{name}' is not defined in fields")
        for flt in self.filters:
            if flt.field not in self.fields:
                raise ValueError(f"filter field '{flt.field}' is not defined in fields")
            if self.fields[flt.field].path is None:
                raise ValueError(f"filter field '{flt.field}' needs a source path, not a constant")
        for column, field_name in self.target.columns.items():
            if field_name not in self.fields:
                raise ValueError(f"column '{column}' maps to unknown field '{field_name}'")
        if self.target.kind == "geometry":
            mapped = set(self.target.columns.values())
            missing = [name for name in self.key if name not in mapped]
            if missing:
                raise ValueError(f"key fields must be mapped to target columns: {missing}")
        return self

    @property
    def on_duplicate(self) -> str:
        """Paging a WFS without a unique key cannot prove the result is complete."""
        return self.duplicate_keys or ("fail" if self.type == "wfs" else "warn")


def split_table(name: str) -> tuple[str, str]:
    """Split ``schema.table`` and reject anything that is not a plain identifier."""
    parts = name.split(".")
    if len(parts) != 2 or not all(IDENT_PATTERN.match(part) for part in parts):
        raise ValueError(f"table must look like schema.table, got '{name}'")
    return parts[0], parts[1]


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise ConfigError(f"Environment variable {name} is not set")
            return os.environ[name]

        return ENV_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    return value


def load_source_file(path: Path) -> SourceConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name}: expected a mapping at the top level")
    try:
        return SourceConfig.model_validate(_expand_env(raw))
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'source'}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigError(f"{path.name}: {details}") from exc


def load_sources(config_dir: Path) -> dict[str, SourceConfig]:
    """Load every ``sources/*.yaml``. A broken file stops the whole command, by design."""
    directory = config_dir / "sources"
    files = sorted(directory.glob("*.yaml"))
    if not files:
        raise ConfigError(f"No source definitions found in {directory}")
    sources: dict[str, SourceConfig] = {}
    for path in files:
        source = load_source_file(path)
        if source.id in sources:
            raise ConfigError(f"{path.name}: duplicate source id '{source.id}'")
        sources[source.id] = source
    return sources
