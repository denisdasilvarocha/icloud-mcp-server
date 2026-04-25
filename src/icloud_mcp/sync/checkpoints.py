"""Sync checkpoint helpers."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any

from icloud_mcp.db.connection import Database
from icloud_mcp.security.redaction import redact_text
from icloud_mcp.util import compact_json, utc_now

MAX_RETRIES = 5
BASE_BACKOFF_SECONDS = 60


def initialize_checkpoints(db: Database, names: list[str]) -> None:
    """Ensure known sync workers have dashboard-visible checkpoints."""

    for name in names:
        db.execute(
            """
            INSERT INTO sync_checkpoints (name, status, last_sync_at, detail_json, retry_count)
            VALUES (?, 'idle', NULL, ?, 0)
            ON CONFLICT(name) DO NOTHING
            """,
            (name, compact_json({"mode": "ready"})),
        )


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


def update_failure_checkpoint(
    db: Database,
    name: str,
    exc: Exception,
    *,
    allow_unredacted: bool = False,
) -> dict[str, Any]:
    """Record a retryable worker failure and return public status details."""

    checkpoint = db.query_one("SELECT retry_count FROM sync_checkpoints WHERE name = ?", (name,))
    retry_count = int((checkpoint or {}).get("retry_count") or 0) + 1
    status = "dead_letter" if retry_count >= MAX_RETRIES else "error"
    backoff_until = None if status == "dead_letter" else _backoff_until(retry_count)
    failure = {
        "status": status,
        "error": exc.__class__.__name__,
        "message": redact_text(str(exc), allow_unredacted=allow_unredacted),
        "last_error": exc.__class__.__name__,
        "retry_count": retry_count,
        "backoff_until": backoff_until,
        "circuit": "open" if status == "dead_letter" else "closed",
    }
    update_checkpoint(db, name, status, failure)
    return failure


def update_worker_result_checkpoint(db: Database, name: str, result: dict[str, Any]) -> dict[str, Any]:
    """Record the final checkpoint for a worker result."""

    if result.get("status") not in {"error", "dead_letter", "backoff"}:
        result["retry_count"] = 0
    update_checkpoint(db, name, _checkpoint_status(result.get("status")), result)
    return result


def update_worker_start_checkpoint(db: Database, name: str) -> None:
    """Mark a worker as running through the scheduler lifecycle."""

    update_checkpoint(db, name, "running", {"mode": "manual_or_background"})


def _progress_cursor(detail: dict[str, Any]) -> str | None:
    value = detail.get("progress_cursor") or detail.get("cursor") or detail.get("last_synced_uid")
    return str(value) if value is not None else None


def _checkpoint_status(status: object) -> str:
    if status in {"skipped", "error", "dead_letter", "backoff"}:
        return str(status)
    return "ok"


def _backoff_until(retry_count: int) -> str:
    delay = BASE_BACKOFF_SECONDS * (2 ** max(0, retry_count - 1)) + random.uniform(0, 5)
    return (datetime.now(tz=UTC) + timedelta(seconds=delay)).replace(microsecond=0).isoformat()
