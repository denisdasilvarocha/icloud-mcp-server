"""Search response schemas."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SearchResultRow:
    """Compact search result row."""

    id: str
    domain: str
    title: str
    snippet: str
    score: float
