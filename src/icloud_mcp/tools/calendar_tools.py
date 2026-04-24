"""FastMCP Calendar tool registration."""

from __future__ import annotations

import uuid
from datetime import datetime

from icloud_mcp.adapters.caldav_calendar import CalDAVCalendarAdapter
from icloud_mcp.config import Settings
from icloud_mcp.db.connection import Database
from icloud_mcp.db.repositories import (
    create_calendar_event,
    first_writable_calendar,
    get_calendar_collection,
    get_calendar_object,
    list_calendars,
    list_events,
    patch_ics,
    update_calendar_event,
    upsert_calendar_collection,
    validate_event_input,
    validate_event_patch,
    view_event,
)
from icloud_mcp.observability.audit import audit_calendar_write
from icloud_mcp.schemas.calendar import CreateEventInput, UpdateEventInput
from icloud_mcp.security.redaction import redact_text
from icloud_mcp.security.secrets import load_icloud_credentials
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
    async def calendar_create_event(input: CreateEventInput) -> dict:
        """Create a calendar event after validating write guardrails."""

        input_data = input.model_dump()
        errors = validate_event_input(input_data)
        if errors:
            return {"status": "invalid", "errors": errors}
        credentials = load_icloud_credentials(settings)
        if not credentials:
            return {"status": "credential_missing", "message": "Configure ICLOUD_APPLE_ID and ICLOUD_APP_PASSWORD"}

        adapter = CalDAVCalendarAdapter()
        calendar = _calendar_for_write(db, settings, adapter, input_data.get("calendar_id"))
        if not calendar:
            return {"status": "sync_required", "message": "No writable remote CalDAV calendar is known"}

        uid = f"cal_evt_{uuid.uuid4().hex}@icloud-mcp.local"
        try:
            remote = adapter.create_event(
                apple_id=credentials.apple_id,
                app_password=credentials.app_password,
                calendar_url=calendar["url"],
                uid=uid,
                title=input_data["title"],
                start=input_data["start"],
                end=input_data["end"],
                timezone=input_data["timezone"],
                location=input_data.get("location"),
                description=input_data.get("description"),
                attendees=input_data.get("attendees") or [],
                recurrence=input_data.get("recurrence"),
                alarms=input_data.get("alarms") or [],
            )
        except Exception as exc:
            return _write_exception_status(exc, settings)
        result = create_calendar_event(
            db,
            calendar_id=calendar["id"],
            title=input_data["title"],
            start=input_data["start"],
            end=input_data["end"],
            timezone=input_data["timezone"],
            location=input_data.get("location"),
            description=input_data.get("description"),
            attendees=input_data.get("attendees") or [],
            recurrence=input_data.get("recurrence"),
            alarms=input_data.get("alarms") or [],
            request_id=input_data.get("request_id"),
            href=remote.href,
            uid=remote.uid,
            etag=remote.etag,
            raw_ics=remote.raw_ics,
            remote_state="created",
        )
        audit_calendar_write(db, "calendar.create_event", result["event_id"], result["status"])
        return result

    @mcp.tool(name="icloud.calendar.update_event", annotations=WRITE_ANNOTATIONS)
    async def calendar_update_event(input: UpdateEventInput) -> dict:
        """Update a full non-recurring event or recurring series."""

        input_data = input.model_dump()
        event_id = input_data.get("event_id")
        patch = input_data.get("patch") or {}
        if not event_id:
            return {"status": "invalid", "errors": ["event_id is required"]}
        errors = validate_event_patch(patch)
        if errors:
            return {"status": "invalid", "errors": errors}
        current = get_calendar_object(db, event_id)
        if not current:
            return {"status": "not_found", "event_id": event_id}
        credentials = load_icloud_credentials(settings)
        if not credentials:
            return {"status": "credential_missing", "message": "Configure ICLOUD_APPLE_ID and ICLOUD_APP_PASSWORD"}
        if str(current["href"]).startswith("local://"):
            return {"status": "sync_required", "message": "Event has no remote CalDAV href. Sync calendar first."}

        raw_ics = _patched_ics(current, patch)
        try:
            remote = CalDAVCalendarAdapter().update_event(
                apple_id=credentials.apple_id,
                app_password=credentials.app_password,
                event_href=current["href"],
                raw_ics=raw_ics,
                expected_etag=input_data.get("etag") or current.get("etag"),
            )
        except Exception as exc:
            return _write_exception_status(exc, settings)
        if isinstance(remote, dict):
            return remote
        result = update_calendar_event(
            db,
            event_id=event_id,
            patch=patch,
            etag=input_data.get("etag"),
            scope=input_data.get("scope", "series"),
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

    credentials = load_icloud_credentials(settings)
    if not credentials:
        return None
    for discovered in adapter.discover(apple_id=credentials.apple_id, app_password=credentials.app_password):
        upsert_calendar_collection(
            db,
            account_id=settings.default_account_id,
            calendar_id=discovered.id,
            url=discovered.url,
            display_name=discovered.display_name,
            color=discovered.color,
            sync_token=discovered.sync_token,
            ctag=discovered.ctag,
            read_only=discovered.read_only,
        )
    if calendar_id:
        calendar = get_calendar_collection(db, calendar_id)
        if calendar and not str(calendar["url"]).startswith("local://") and not bool(calendar["read_only"]):
            return calendar
    return first_writable_calendar(db)


def _patched_ics(current: dict, patch: dict) -> str:
    return patch_ics(current["raw_ics"], patch, current)


def _write_exception_status(exc: Exception, settings: Settings) -> dict:
    message = redact_text(str(exc), allow_unredacted=settings.allow_unredacted_debug) or exc.__class__.__name__
    lowered = message.casefold()
    if any(term in lowered for term in ["auth", "credential", "password", "unauthorized", "forbidden"]):
        return {"status": "credential_revoked_or_expired", "message": message}
    return {"status": "connectivity_error", "message": message, "queued": False}
