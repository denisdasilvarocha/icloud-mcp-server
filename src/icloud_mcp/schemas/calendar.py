"""Calendar schemas."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CalendarWriteResult:
    """Calendar write result."""

    status: str
    event_id: str
    etag: str | None
