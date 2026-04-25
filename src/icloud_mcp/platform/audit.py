"""Audit logging without body or secret content."""

from __future__ import annotations

import uuid

from icloud_mcp.platform.util import utc_now
from icloud_mcp.storage.connection import Database


def audit_calendar_write(db: Database, event_type: str, object_id: str, status: str) -> None:
    """Record calendar write summary without sensitive body fields."""

    db.execute(
        """
        INSERT INTO audit_events (id, event_type, object_id, summary, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (f"audit_{uuid.uuid4().hex}", event_type, object_id, f"{event_type} {status}", utc_now()),
    )
