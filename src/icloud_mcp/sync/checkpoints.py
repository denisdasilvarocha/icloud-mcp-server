"""Sync checkpoint helpers."""

from __future__ import annotations

from icloud_mcp.db.connection import Database
from icloud_mcp.util import compact_json, utc_now


def update_checkpoint(db: Database, name: str, status: str, detail: dict | None = None) -> None:
    """Upsert one sync checkpoint."""

    db.execute(
        """
        INSERT INTO sync_checkpoints (name, status, last_sync_at, detail_json)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
          status = excluded.status,
          last_sync_at = excluded.last_sync_at,
          detail_json = excluded.detail_json
        """,
        (name, status, utc_now(), compact_json(detail or {})),
    )
