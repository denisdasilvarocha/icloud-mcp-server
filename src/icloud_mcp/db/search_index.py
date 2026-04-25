"""Search index read interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from icloud_mcp.db import repositories
from icloud_mcp.db.connection import Database


@dataclass(frozen=True)
class SearchIndexQuery:
    """Resolved query inputs for local search index reads."""

    query: str
    domains: list[str]
    limit: int
    offset: int
    snippet_chars: int
    start: str | None = None
    end: str | None = None
    person: str | None = None


def search_index(db: Database, query: SearchIndexQuery) -> list[dict[str, Any]]:
    """Read compact search rows from the local search index."""

    return repositories.search_documents(
        db,
        query=query.query,
        domains=query.domains,
        limit=query.limit,
        offset=query.offset,
        snippet_chars=query.snippet_chars,
        start=query.start,
        end=query.end,
        person=query.person,
    )
