"""Repository functions for local cache state and sync freshness."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from icloud_mcp.platform.config import Settings
from icloud_mcp.platform.util import parse_json, utc_now
from icloud_mcp.storage.connection import Database


def ensure_defaults(db: Database, settings: Settings) -> None:
    """Ensure local account and default collections exist before sync."""

    now = utc_now()
    db.execute(
        """
        INSERT OR IGNORE INTO accounts (id, apple_id_hash, display_name, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (settings.default_account_id, "not-configured", "Local iCloud cache", now),
    )
    db.execute(
        """
        INSERT OR IGNORE INTO calendar_collections
          (id, account_id, url, display_name, read_only, last_sync_at)
        VALUES (?, ?, ?, ?, 0, NULL)
        """,
        (
            settings.default_calendar_id,
            settings.default_account_id,
            "local://calendar/primary",
            "Calendar",
        ),
    )
    db.execute(
        """
        INSERT OR IGNORE INTO addressbooks
          (id, account_id, url, display_name, last_sync_at)
        VALUES (?, ?, ?, ?, NULL)
        """,
        (
            settings.default_addressbook_id,
            settings.default_account_id,
            "local://contacts/default",
            "Contacts",
        ),
    )
    db.execute(
        """
        INSERT OR IGNORE INTO index_state (id, generation, updated_at)
        VALUES (1, 0, ?)
        """,
        (now,),
    )


def bump_index_generation(db: Database) -> int:
    """Increment and return index generation."""

    now = utc_now()
    db.execute("UPDATE index_state SET generation = generation + 1, updated_at = ? WHERE id = 1", (now,))
    row = db.query_one("SELECT generation FROM index_state WHERE id = 1")
    return int(row["generation"]) if row else 0


def index_generation(db: Database) -> int:
    """Return current index generation."""

    row = db.query_one("SELECT generation FROM index_state WHERE id = 1")
    return int(row["generation"]) if row else 0


def freshness(db: Database) -> dict[str, str | None]:
    """Return sync freshness per domain."""

    mail = db.query_one("SELECT MAX(last_sync_at) AS value FROM mailboxes")
    calendar = db.query_one("SELECT MAX(last_sync_at) AS value FROM calendar_collections")
    contacts = db.query_one("SELECT MAX(last_sync_at) AS value FROM addressbooks")
    backfill = db.query_one(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN backfill_status = 'complete' THEN 1 ELSE 0 END) AS complete
        FROM mailboxes
        """
    )
    return {
        "mail_last_sync": mail["value"] if mail else None,
        "calendar_last_sync": calendar["value"] if calendar else None,
        "contacts_last_sync": contacts["value"] if contacts else None,
        "mail_backfill_status": _mail_backfill_status(backfill),
    }


def freshness_status(db: Database, stale_after_seconds: int) -> dict[str, dict[str, Any]]:
    """Return freshness timestamps plus healthy/stale classification."""

    values = freshness(db)
    now = datetime.now(tz=UTC)
    statuses: dict[str, dict[str, Any]] = {}
    for domain, key in [
        ("mail", "mail_last_sync"),
        ("calendar", "calendar_last_sync"),
        ("contacts", "contacts_last_sync"),
    ]:
        synced_at = values.get(key)
        age_seconds = None
        status = "never_synced"
        reason = "no successful sync timestamp"
        if synced_at:
            age_seconds = max(0, int((now - datetime.fromisoformat(str(synced_at))).total_seconds()))
            status = "stale" if age_seconds > stale_after_seconds else "healthy"
            reason = f"older than {stale_after_seconds}s" if status == "stale" else "within freshness threshold"
        statuses[domain] = {
            "last_sync_at": synced_at,
            "age_seconds": age_seconds,
            "status": status,
            "reason": reason,
        }
    statuses["mail"]["backfill_status"] = values.get("mail_backfill_status")
    return statuses


def sync_status(db: Database, stale_after_seconds: int = 86400) -> dict[str, Any]:
    """Return sync checkpoint state."""

    checkpoints = db.query(
        """
        SELECT name, status, last_sync_at, last_error, retry_count, backoff_until, progress_cursor, detail_json
        FROM sync_checkpoints
        ORDER BY name
        """
    )
    return {
        "index_generation": index_generation(db),
        "index_freshness": freshness(db),
        "freshness_status": freshness_status(db, stale_after_seconds),
        "workers": {
            row["name"]: {
                "status": row["status"],
                "last_sync_at": row["last_sync_at"],
                "last_error": row.get("last_error"),
                "retry_count": row.get("retry_count") or 0,
                "backoff_until": row.get("backoff_until"),
                "progress_cursor": row.get("progress_cursor"),
                "detail": parse_json(row["detail_json"], {}),
            }
            for row in checkpoints
        },
    }


def _mail_backfill_status(row: dict[str, Any] | None) -> str:
    if not row or not row.get("total"):
        return "not_started"
    complete = int(row.get("complete") or 0)
    total = int(row.get("total") or 0)
    if complete == total:
        return "complete"
    if complete > 0:
        return "partial"
    return "not_started"
