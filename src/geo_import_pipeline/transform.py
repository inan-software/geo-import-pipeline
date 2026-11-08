"""Field mapping, type conversion, required fields, keys and content hashes."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime
from typing import Any, Callable

from .config import CsvConfig, FieldConfig, FilterConfig, SourceConfig
from .model import RawRecord, StagedRow, WarningLog

KEY_SEPARATOR = "\x1f"
Predicate = Callable[[str | None], bool]


def clean_text(value: str | None, pad: int | None = None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    return value.zfill(pad) if pad else value


def compile_filter(flt: FilterConfig, spec: FieldConfig) -> Predicate:
    """Build a predicate on the raw value, honouring padding so prefixes see the full key."""
    if flt.equals is not None:
        wanted = flt.equals
        return lambda value: clean_text(value, spec.pad) == wanted
    if flt.in_ is not None:
        allowed = set(flt.in_)
        return lambda value: clean_text(value, spec.pad) in allowed
    prefix = flt.prefix
    return lambda value: (cleaned := clean_text(value, spec.pad)) is not None and cleaned.startswith(prefix)


def compile_filters(source: SourceConfig) -> list[tuple[str, Predicate]]:
    """Filters as (source path, predicate). Constant fields cannot be filtered on."""
    compiled = []
    for flt in source.filters:
        spec = source.fields[flt.field]
        if spec.path is None:
            raise ValueError(f"filter field '{flt.field}' has no source path")
        compiled.append((spec.path, compile_filter(flt, spec)))
    return compiled


class Transformer:
    def __init__(self, source: SourceConfig, warnings: WarningLog) -> None:
        self.source = source
        self.warnings = warnings
        self._filters = compile_filters(source)
        csv_cfg: CsvConfig | None = source.csv
        self._decimal = csv_cfg.decimal if csv_cfg else "."
        self._thousands = csv_cfg.thousands if csv_cfg else None
        self._nulls = frozenset(csv_cfg.null_values) if csv_cfg else frozenset({""})
        self._required = set(source.key) | {name for name, spec in source.fields.items() if spec.required}
        self._key_fields = list(source.key)
        self._value_fields = [name for name in source.fields if name not in set(source.key)]
        self._row_no = 0

    def matches(self, raw: RawRecord) -> bool:
        return all(predicate(raw.values.get(path)) for path, predicate in self._filters)

    def apply(self, raw: RawRecord) -> StagedRow | None:
        """Map one record. Returns None if it has to be skipped; the reason goes to the warnings."""
        mapped: dict[str, Any] = {}
        for name, spec in self.source.fields.items():
            if spec.const is not None:
                mapped[name] = spec.const
                continue
            value = raw.values.get(spec.path)
            if value in self._nulls:
                value = None
            try:
                mapped[name] = self._convert(value, spec)
            except ValueError:
                mapped[name] = None
                self.warnings.add(
                    "conversion_failed",
                    f"Value of field '{name}' could not be converted to {spec.type}",
                    f"{raw.ref}: {value!r}",
                )

        missing = [name for name in self._required if mapped.get(name) is None]
        if missing:
            self.warnings.add(
                "missing_required_field",
                f"Record skipped, required field(s) missing: {', '.join(sorted(missing))}",
                raw.ref or None,
            )
            return None

        key_text = KEY_SEPARATOR.join(str(mapped[name]) for name in self._key_fields)
        content = {name: mapped[name] for name in self._value_fields}
        self._row_no += 1
        return StagedRow(
            row_no=self._row_no,
            key_hash=hashlib.sha256(key_text.encode("utf-8")).digest(),
            key_text=key_text.replace(KEY_SEPARATOR, " | "),
            content_hash=hashlib.sha256(
                json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).digest(),
            attrs=json.dumps(mapped, ensure_ascii=False),
            gml=raw.gml,
        )

    def _convert(self, value: str | None, spec: FieldConfig) -> Any:
        if spec.type == "text":
            return clean_text(value, spec.pad)
        value = clean_text(value)
        if value is None:
            return None
        if spec.type == "integer":
            return int(self._normalize_number(value))
        if spec.type == "float":
            number = float(self._normalize_number(value))
            if not math.isfinite(number):  # "nan" and "inf" parse, but are not values.
                raise ValueError(f"not a finite number: {value}")
            return number
        return _parse_date(value, spec.date_format)

    def _normalize_number(self, value: str) -> str:
        if self._thousands:
            value = value.replace(self._thousands, "")
        if self._decimal != ".":
            value = value.replace(self._decimal, ".")
        return value


def _parse_date(value: str, date_format: str | None) -> str:
    if date_format:
        return datetime.strptime(value, date_format).date().isoformat()
    return date.fromisoformat(value[:10]).isoformat()
