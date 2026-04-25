"""FastMCP Calendar tool registration."""

from __future__ import annotations

from datetime import datetime

from icloud_mcp.config import Settings
from icloud_mcp.db.calendar_repository import (
    list_calendars,
    list_events,
    view_event,
)
from icloud_mcp.db.connection import Database
from icloud_mcp.schemas.calendar import CreateEventInput, UpdateEventInput
from icloud_mcp.services import calendar_write as calendar_write_service
from icloud_mcp.tools.boundary import bounded_int, cursor_offset, cursor_state_or_error, not_found
from icloud_mcp.tools.search_tools import READ_ANNOTATIONS

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

        cursor_payload, error = cursor_state_or_error(cursor, settings.cursor_secret)
        if error:
            return error
        return list_events(
            db,
            calendar_ids=calendar_ids,
            start=start.isoformat() if start else None,
            end=end.isoformat() if end else None,
            limit=bounded_int(limit, minimum=1, maximum=200),
            offset=cursor_offset(cursor_payload),
            cursor_secret=settings.cursor_secret,
        )

    @mcp.tool(name="icloud.calendar.view_event", annotations=READ_ANNOTATIONS)
    async def calendar_view_event(event_id: str, include_raw_ics: bool = False) -> dict:
        """View one cached calendar event."""

        result = view_event(db, event_id=event_id, include_raw_ics=include_raw_ics)
        return result or not_found("event_id", event_id)

    @mcp.tool(name="icloud.calendar.create_event", annotations=WRITE_ANNOTATIONS)
    async def calendar_create_event(input: CreateEventInput) -> dict:
        """Create a calendar event after validating write guardrails."""

        return calendar_write_service.CalendarWriteService(db, settings).create_event(input.model_dump())

    @mcp.tool(name="icloud.calendar.update_event", annotations=WRITE_ANNOTATIONS)
    async def calendar_update_event(input: UpdateEventInput) -> dict:
        """Update a full non-recurring event or recurring series."""

        return calendar_write_service.CalendarWriteService(db, settings).update_event(input.model_dump())


def _calendar_for_write(*args: object, **kwargs: object) -> dict | None:
    return calendar_write_service.calendar_for_write(*args, **kwargs)


def _patched_ics(*args: object, **kwargs: object) -> str:
    return calendar_write_service.patched_ics(*args, **kwargs)


def _write_exception_status(*args: object, **kwargs: object) -> dict:
    return calendar_write_service.write_exception_status(*args, **kwargs)
