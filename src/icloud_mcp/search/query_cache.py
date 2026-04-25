"""Search query cache repository interface."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from icloud_mcp.platform.util import compact_json, parse_json, utc_now
from icloud_mcp.storage.connection import Database


def query_cache_get(db: Database, key: str, generation: int) -> dict[str, Any] | None:
    """Return a valid cached search response."""

    now = utc_now()
    row = db.query_one(
        """
        SELECT value_json
        FROM query_cache
        WHERE key = ? AND index_generation = ? AND expires_at > ?
        """,
        (key, generation, now),
    )
    return parse_json(row["value_json"], {}) if row else None


def query_cache_set(db: Database, key: str, value: dict[str, Any], generation: int, ttl_seconds: int = 300) -> None:
    """Store a short-lived search response cache entry."""

    expires_at = (datetime.now(tz=UTC) + timedelta(seconds=ttl_seconds)).replace(microsecond=0).isoformat()
    db.execute(
        """
        INSERT INTO query_cache (key, value_json, expires_at, index_generation)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
          value_json = excluded.value_json,
          expires_at = excluded.expires_at,
          index_generation = excluded.index_generation
        """,
        (key, compact_json(value), expires_at, generation),
    )
