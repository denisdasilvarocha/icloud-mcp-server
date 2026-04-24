"""FastMCP Calendar tool registration."""

from __future__ import annotations

import uuid
from datetime import datetime

from icloud_mcp.adapters.caldav_calendar import CalDAVCalendarAdapter
from icloud_mcp.config import Settings
from icloud_mcp.db.connection import Database
from icloud_mcp.db.repositories import (
    build_ics,
    create_calendar_event,
    first_writable_calendar,
    get_calendar_collection,
    get_calendar_object,
    list_calendars,
    list_events,
    update_calendar_event,
    upsert_calendar_collection,
    validate_event_input,
    validate_event_patch,
    view_event,
)
from icloud_mcp.observability.audit import audit_calendar_write
from icloud_mcp.tools.search_tools import READ_ANNOTATIONS
from icloud_mcp.util import decode_cursor

WRITE_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": True,
}


def register_calendar_tools(mcp: object, db: Database, settings: Settings) -> None:
    """Register calendar read and write tools."""

    @mcp.tool(name="icloud.calendar.list_calendars", annotations=READ_ANNOTATIONS)
    async def calendar_list_calendars() -> dict:
        """List known calendars."""

        return {"calendars": list_calendars(db)}

    @mcp.tool(name="icloud.calendar.list_events", annotations=READ_ANNOTATIONS)
    async def calendar_list_events(
        calendar_ids: list[str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict:
        """List cached calendar events by time range."""

        cursor_payload = decode_cursor(cursor, settings.cursor_secret)
        return list_events(
            db,
            calendar_ids=calendar_ids,
            start=start.isoformat() if start else None,
            end=end.isoformat() if end else None,
            limit=max(1, min(limit, 200)),
            offset=int(cursor_payload.get("offset", 0)),
            cursor_secret=settings.cursor_secret,
        )

    @mcp.tool(name="icloud.calendar.view_event", annotations=READ_ANNOTATIONS)
    async def calendar_view_event(event_id: str, include_raw_ics: bool = False) -> dict:
        """View one cached calendar event."""

        result = view_event(db, event_id=event_id, include_raw_ics=include_raw_ics)
        return result or {"status": "not_found", "event_id": event_id}

    @mcp.tool(name="icloud.calendar.create_event", annotations=WRITE_ANNOTATIONS)
    async def calendar_create_event(input: dict) -> dict:
        """Create a calendar event after validating write guardrails."""

        errors = validate_event_input(input)
        if errors:
            return {"status": "invalid", "errors": errors}
        if not settings.apple_id or not settings.app_password:
            return {"status": "credential_missing", "message": "Configure ICLOUD_APPLE_ID and ICLOUD_APP_PASSWORD"}

        adapter = CalDAVCalendarAdapter()
        calendar = _calendar_for_write(db, settings, adapter, input.get("calendar_id"))
        if not calendar:
            return {"status": "sync_required", "message": "No writable remote CalDAV calendar is known"}

        uid = f"cal_evt_{uuid.uuid4().hex}@icloud-mcp.local"
        remote = adapter.create_event(
            apple_id=settings.apple_id,
            app_password=settings.app_password,
            calendar_url=calendar["url"],
            uid=uid,
            title=input["title"],
            start=input["start"],
            end=input["end"],
            timezone=input["timezone"],
            location=input.get("location"),
            description=input.get("description"),
            attendees=input.get("attendees") or [],
            recurrence=input.get("recurrence"),
            alarms=input.get("alarms") or [],
        )
        result = create_calendar_event(
            db,
            calendar_id=calendar["id"],
            title=input["title"],
            start=input["start"],
            end=input["end"],
            timezone=input["timezone"],
            location=input.get("location"),
            description=input.get("description"),
            attendees=input.get("attendees") or [],
            recurrence=input.get("recurrence"),
            alarms=input.get("alarms") or [],
            request_id=input.get("request_id"),
            href=remote.href,
            uid=remote.uid,
            etag=remote.etag,
            raw_ics=remote.raw_ics,
            remote_state="created",
        )
        audit_calendar_write(db, "calendar.create_event", result["event_id"], result["status"])
        return result

    @mcp.tool(name="icloud.calendar.update_event", annotations=WRITE_ANNOTATIONS)
    async def calendar_update_event(input: dict) -> dict:
        """Update a full non-recurring event or recurring series."""

        event_id = input.get("event_id")
        patch = input.get("patch") or {}
        if not event_id:
            return {"status": "invalid", "errors": ["event_id is required"]}
        errors = validate_event_patch(patch)
        if errors:
            return {"status": "invalid", "errors": errors}
        current = get_calendar_object(db, event_id)
        if not current:
            return {"status": "not_found", "event_id": event_id}
        if not settings.apple_id or not settings.app_password:
            return {"status": "credential_missing", "message": "Configure ICLOUD_APPLE_ID and ICLOUD_APP_PASSWORD"}
        if str(current["href"]).startswith("local://"):
            return {"status": "sync_required", "message": "Event has no remote CalDAV href. Sync calendar first."}

        raw_ics = _patched_ics(current, patch)
        remote = CalDAVCalendarAdapter().update_event(
            apple_id=settings.apple_id,
            app_password=settings.app_password,
            event_href=current["href"],
            raw_ics=raw_ics,
            expected_etag=input.get("etag") or current.get("etag"),
        )
        if isinstance(remote, dict):
            return remote
        result = update_calendar_event(
            db,
            event_id=event_id,
            patch=patch,
            etag=input.get("etag"),
            scope=input.get("scope", "series"),
            etag_override=remote.etag,
            raw_ics_override=remote.raw_ics,
        )
        audit_calendar_write(db, "calendar.update_event", event_id, result["status"])
        return result


def _calendar_for_write(
    db: Database,
    settings: Settings,
    adapter: CalDAVCalendarAdapter,
    calendar_id: str | None,
) -> dict | None:
    if calendar_id:
        calendar = get_calendar_collection(db, calendar_id)
        if calendar and not str(calendar["url"]).startswith("local://") and not bool(calendar["read_only"]):
            return calendar

    for discovered in adapter.discover(apple_id=settings.apple_id or "", app_password=settings.app_password or ""):
        upsert_calendar_collection(
            db,
            account_id=settings.default_account_id,
            calendar_id=discovered.id,
            url=discovered.url,
            display_name=discovered.display_name,
            color=discovered.color,
            read_only=discovered.read_only,
        )
    if calendar_id:
        calendar = get_calendar_collection(db, calendar_id)
        if calendar and not str(calendar["url"]).startswith("local://") and not bool(calendar["read_only"]):
            return calendar
    return first_writable_calendar(db)


def _patched_ics(current: dict, patch: dict) -> str:
    attendees = patch.get("attendees")
    if attendees is None:
        from icloud_mcp.util import parse_json

        attendees = parse_json(current.get("attendees_json"), [])
    return build_ics(
        uid=current["uid"],
        title=patch.get("title", current["summary"]),
        start=patch.get("start", current["dtstart"]),
        end=patch.get("end", current["dtend"]),
        timezone=patch.get("timezone", current["timezone"]),
        location=patch.get("location", current["location"]),
        description=patch.get("description", current["description"]),
        attendees=attendees,
        recurrence=patch.get("recurrence"),
        alarms=[],
    )
