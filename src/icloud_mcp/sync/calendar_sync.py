"""Calendar sync worker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from icloud_mcp.adapters.caldav_calendar import CalDAVCalendarAdapter
from icloud_mcp.config import Settings
from icloud_mcp.db.connection import Database
from icloud_mcp.db.repositories import upsert_calendar_collection, upsert_calendar_object
from icloud_mcp.sync.checkpoints import update_checkpoint
from icloud_mcp.util import utc_now


@dataclass
class CalendarSyncWorker:
    """Synchronize iCloud Calendar through CalDAV."""

    db: Database
    settings: Settings
    adapter: CalDAVCalendarAdapter | None = None

    name = "calendar_sync_worker"

    def run_once(self) -> dict:
        """Run one calendar sync cycle."""

        if not self.settings.apple_id or not self.settings.app_password:
            result = {"status": "skipped", "reason": "credentials_missing"}
            update_checkpoint(self.db, self.name, "skipped", result)
            return result

        adapter = self.adapter or CalDAVCalendarAdapter()
        now_dt = datetime.now(tz=UTC)
        start = (now_dt - timedelta(days=31 * self.settings.calendar_past_months)).date()
        end = (now_dt + timedelta(days=31 * self.settings.calendar_future_months)).date()
        calendars, events = adapter.sync_events(
            apple_id=self.settings.apple_id,
            app_password=self.settings.app_password,
            start=start,
            end=end,
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
        result = {"status": "ok", "calendars": len(calendars), "events": len(events)}
        update_checkpoint(self.db, self.name, "ok", result)
        return result
