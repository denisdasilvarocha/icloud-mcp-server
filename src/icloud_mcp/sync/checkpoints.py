"""Sync checkpoint helpers."""

from __future__ import annotations

from typing import Any

from icloud_mcp.db.connection import Database
from icloud_mcp.util import compact_json, utc_now


def update_checkpoint(db: Database, name: str, status: str, detail: dict | None = None) -> None:
    """Upsert one sync checkpoint."""

    detail = detail or {}
    db.execute(
        """
        INSERT INTO sync_checkpoints
          (name, status, last_sync_at, last_error, retry_count, backoff_until, progress_cursor, detail_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
          status = excluded.status,
          last_sync_at = excluded.last_sync_at,
          last_error = excluded.last_error,
          retry_count = excluded.retry_count,
          backoff_until = excluded.backoff_until,
          progress_cursor = excluded.progress_cursor,
          detail_json = excluded.detail_json
        """,
        (
            name,
            status,
            utc_now(),
            detail.get("last_error") or detail.get("error"),
            int(detail.get("retry_count") or 0),
            detail.get("backoff_until"),
            _progress_cursor(detail),
            compact_json(detail),
        ),
    )


def _progress_cursor(detail: dict[str, Any]) -> str | None:
    value = detail.get("progress_cursor") or detail.get("cursor") or detail.get("last_synced_uid")
    return str(value) if value is not None else None
