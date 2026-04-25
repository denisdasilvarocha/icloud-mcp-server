"""Minimal metrics value objects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from icloud_mcp.db.connection import Database
from icloud_mcp.util import compact_json, parse_json, utc_now


@dataclass(frozen=True)
class TimingMetric:
    """Simple timing metric shape for future middleware."""

    name: str
    duration_ms: float


def record_metric(db: Database, name: str, value: float, tags: dict[str, Any] | None = None) -> None:
    """Record a compact local metric row."""

    db.execute(
        """
        INSERT INTO metrics (name, value, tags_json, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (name, value, compact_json(tags or {}), utc_now()),
    )


def metrics_snapshot(db: Database, limit: int = 100) -> dict[str, Any]:
    """Return recent metrics and aggregate counters."""

    rows = db.query(
        """
        SELECT name, value, tags_json, created_at
        FROM metrics
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )
    total_rows = db.query(
        """
        SELECT name, SUM(value) AS total
        FROM metrics
        GROUP BY name
        """
    )
    totals = {row["name"]: float(row["total"]) for row in total_rows}
    return {
        "totals": {name: round(value, 3) for name, value in sorted(totals.items())},
        "recent": [
            {
                "name": row["name"],
                "value": row["value"],
                "tags": parse_json(row["tags_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ],
    }
