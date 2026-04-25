"""Calendar repository interface."""

from __future__ import annotations

from icloud_mcp.db.repositories import build_ics as build_ics
from icloud_mcp.db.repositories import create_calendar_event as create_calendar_event
from icloud_mcp.db.repositories import first_writable_calendar as first_writable_calendar
from icloud_mcp.db.repositories import get_calendar_collection as get_calendar_collection
from icloud_mcp.db.repositories import get_calendar_object as get_calendar_object
from icloud_mcp.db.repositories import index_calendar_event as index_calendar_event
from icloud_mcp.db.repositories import list_calendars as list_calendars
from icloud_mcp.db.repositories import list_events as list_events
from icloud_mcp.db.repositories import patch_ics as patch_ics
from icloud_mcp.db.repositories import tombstone_calendar_object as tombstone_calendar_object
from icloud_mcp.db.repositories import update_calendar_event as update_calendar_event
from icloud_mcp.db.repositories import upsert_calendar_collection as upsert_calendar_collection
from icloud_mcp.db.repositories import upsert_calendar_object as upsert_calendar_object
from icloud_mcp.db.repositories import validate_event_input as validate_event_input
from icloud_mcp.db.repositories import validate_event_patch as validate_event_patch
from icloud_mcp.db.repositories import view_event as view_event

__all__ = [
    "build_ics",
    "create_calendar_event",
    "first_writable_calendar",
    "get_calendar_collection",
    "get_calendar_object",
    "index_calendar_event",
    "list_calendars",
    "list_events",
    "patch_ics",
    "tombstone_calendar_object",
    "update_calendar_event",
    "upsert_calendar_collection",
    "upsert_calendar_object",
    "validate_event_input",
    "validate_event_patch",
    "view_event",
]
