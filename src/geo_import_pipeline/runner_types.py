"""The outcome of one source within a run."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .diff import DiffResult
from .model import SourceStats


@dataclass
class SourceResult:
    source_id: str
    source_type: str = ""
    target_kind: str = ""
    status: str = "failed"
    error: str | None = None
    stats: SourceStats = field(default_factory=SourceStats)
    version: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    fetched_at: datetime | None = None
    valid: int = 0
    diff: DiffResult | None = None

    @property
    def duration_s(self) -> float | None:
        if self.started_at and self.finished_at:
            return round((self.finished_at - self.started_at).total_seconds(), 3)
        return None
