"""Calendar sync worker."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urldefrag, urljoin

from icloud_mcp.calendar.adapter import CalDAVCalendarAdapter
from icloud_mcp.calendar.cache import (
    tombstone_calendar_object,
    upsert_calendar_collection,
    upsert_calendar_object,
)
from icloud_mcp.platform.config import Settings
from icloud_mcp.platform.secrets import load_icloud_credentials
from icloud_mcp.platform.util import utc_now
from icloud_mcp.storage.connection import Database
from icloud_mcp.sync.checkpoints import update_checkpoint, update_failure_checkpoint
from icloud_mcp.sync.delta import sync_delta_first


@dataclass
class CalendarSyncWorker:
    """Synchronize iCloud Calendar through CalDAV."""

    db: Database
    settings: Settings
    adapter: CalDAVCalendarAdapter | None = None

    name = "calendar_sync_worker"

    def run_once(self) -> dict:
        """Run one calendar sync cycle."""

        credentials = load_icloud_credentials(self.settings)
        if not credentials:
            result = {"status": "skipped", "reason": "credentials_missing"}
            update_checkpoint(self.db, self.name, "skipped", result)
            return result

        try:
            adapter = self.adapter or CalDAVCalendarAdapter()
            now_dt = datetime.now(tz=UTC)
            start = (now_dt - timedelta(days=31 * self.settings.calendar_past_months)).date()
            end = (now_dt + timedelta(days=31 * self.settings.calendar_future_months)).date()
            calendars, events, full_sync_calendar_ids, deleted_hrefs = self._sync_with_tokens(
                adapter, credentials.apple_id, credentials.app_password, start, end
            )
            now = utc_now()
            for calendar in calendars:
                upsert_calendar_collection(
                    self.db,
                    account_id=self.settings.default_account_id,
                    calendar_id=calendar.id,
                    url=calendar.url,
                    display_name=calendar.display_name,
                    color=calendar.color,
                    sync_token=calendar.sync_token,
                    ctag=calendar.ctag,
                    read_only=calendar.read_only,
                    last_sync_at=now,
                )
            for event in events:
                upsert_calendar_object(
                    self.db,
                    calendar_id=event.calendar_id,
                    event_id=event.id,
                    href=event.href,
                    uid=event.uid,
                    etag=event.etag,
                    raw_ics=event.raw_ics,
                    summary=event.summary,
                    description=event.description,
                    location=event.location,
                    dtstart=event.dtstart,
                    dtend=event.dtend,
                    timezone=event.timezone,
                    attendees=event.attendees,
                    organizer=event.organizer,
                    rrule=event.rrule,
                    recurrence_id=event.recurrence_id,
                    status=event.status,
                )
            for href in deleted_hrefs:
                rows = self.db.query(
                    """
                    SELECT id
                    FROM calendar_objects
                    WHERE deleted_at IS NULL
                      AND (href = ? OR href LIKE ? ESCAPE '\\')
                    """,
                    (href, f"{_escape_like(href)}#%"),
                )
                for row in rows:
                    tombstone_calendar_object(self.db, row["id"])
            synced_by_calendar: dict[str, set[str]] = {}
            for event in events:
                if event.calendar_id in full_sync_calendar_ids:
                    synced_by_calendar.setdefault(event.calendar_id, set()).add(event.id)
            for calendar_id, synced_ids in synced_by_calendar.items():
                existing = self.db.query(
                    """
                    SELECT id
                    FROM calendar_objects
                    WHERE calendar_id = ?
                      AND deleted_at IS NULL
                      AND dtend >= ?
                      AND dtstart <= ?
                    """,
                    (calendar_id, start.isoformat(), end.isoformat()),
                )
                for row in existing:
                    if row["id"] not in synced_ids:
                        tombstone_calendar_object(self.db, row["id"])
            result = {"status": "ok", "calendars": len(calendars), "events": len(events)}
            update_checkpoint(self.db, self.name, "ok", result)
            return result
        except Exception as exc:
            return update_failure_checkpoint(
                self.db,
                self.name,
                exc,
                allow_unredacted=self.settings.allow_unredacted_debug,
            )

    def _sync_with_tokens(
        self,
        adapter: CalDAVCalendarAdapter,
        apple_id: str,
        app_password: str,
        start: date,
        end: date,
    ) -> tuple[list, list, set[str], list[str]]:
        calendars = adapter.discover(apple_id=apple_id, app_password=app_password)
        result = sync_delta_first(
            db=self.db,
            collections=calendars,
            existing_sql="SELECT sync_token, ctag FROM calendar_collections WHERE url = ?",
            sync_changes=lambda calendar, sync_token: adapter.sync_event_changes(
                apple_id=apple_id,
                app_password=app_password,
                calendar_id=calendar.id,
                calendar_url=calendar.url,
                sync_token=sync_token,
            ),
            full_sync_items=lambda: adapter.sync_events(
                apple_id=apple_id,
                app_password=app_password,
                start=start,
                end=end,
            )[1],
            item_collection_id=lambda event: event.calendar_id,
            deleted_href=lambda calendar, href: _absolute_member_url(calendar.url, href),
            collection_with_sync_token=lambda calendar, sync_token: replace(calendar, sync_token=sync_token),
        )
        return result.collections, result.items, result.full_sync_collection_ids, result.deleted_hrefs


def _absolute_member_url(collection_url: str, href: str) -> str:
    return urldefrag(urljoin(collection_url, href)).url


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
