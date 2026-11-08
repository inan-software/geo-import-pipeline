"""Shared data structures and errors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

MAX_SAMPLES = 20


class SourceError(Exception):
    """A source failed. Nothing of it is written to the target tables."""


class FetchError(SourceError):
    """The source could not be retrieved."""


class IncompleteFetchError(SourceError):
    """The retrieval finished but is not provably complete, so nothing may be deleted."""


class TargetError(SourceError):
    """The target tables do not match the configuration."""


@dataclass(frozen=True, slots=True)
class RawRecord:
    """One record as delivered by a source, keyed by the configured source paths."""

    values: Mapping[str, str | None]
    gml: str | None = None
    ref: str = ""


@dataclass(slots=True)
class StagedRow:
    row_no: int
    key_hash: bytes
    key_text: str
    content_hash: bytes
    attrs: str
    gml: str | None


class WarningLog:
    """Collects warnings per kind, so a million bad rows do not produce a million lines."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], dict] = {}

    def add(self, code: str, message: str, sample: str | None = None, count: int = 1) -> None:
        entry = self._entries.setdefault((code, message), {"code": code, "message": message, "count": 0, "samples": []})
        entry["count"] += count
        if sample is not None and len(entry["samples"]) < MAX_SAMPLES:
            entry["samples"].append(sample)

    def as_list(self) -> list[dict]:
        return list(self._entries.values())

    @property
    def total(self) -> int:
        return sum(entry["count"] for entry in self._entries.values())


@dataclass(slots=True)
class SourceStats:
    read: int = 0
    filtered_out: int = 0
    skipped: int = 0
    written: int = 0
    warnings: WarningLog = field(default_factory=WarningLog)
