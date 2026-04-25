"""Calendar write orchestration for remote CalDAV and local cache."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from icloud_mcp.adapters.caldav_calendar import CalDAVCalendarAdapter
from icloud_mcp.config import Settings
from icloud_mcp.db.calendar_repository import (
    create_calendar_event,
    first_writable_calendar,
    get_calendar_collection,
    get_calendar_object,
    patch_ics,
    update_calendar_event,
    upsert_calendar_collection,
    validate_event_input,
    validate_event_patch,
)
from icloud_mcp.db.connection import Database
from icloud_mcp.observability.audit import audit_calendar_write
from icloud_mcp.security.redaction import redact_text
from icloud_mcp.security.secrets import load_icloud_credentials
from icloud_mcp.tools.boundary import not_found
from icloud_mcp.util import parse_json, utc_now

_CREATE_OPERATION = "calendar.create_event"
_UPDATE_OPERATION = "calendar.update_event"


@dataclass
class CalendarWriteService:
    """Remote-first Calendar writes with local-cache persistence."""

    db: Database
    settings: Settings

    def create_event(self, input_data: dict[str, Any]) -> dict:
        """Create a calendar event after validating write guardrails."""

        errors = validate_event_input(input_data)
        if errors:
            return _invalid_status(errors)
        credentials = load_icloud_credentials(self.settings)
        if not credentials:
            return _credential_missing_status()

        adapter = CalDAVCalendarAdapter()
        calendar = calendar_for_write(self.db, self.settings, adapter, input_data.get("calendar_id"))
        if not calendar:
            return _calendar_sync_required_status()

        request_id = input_data.get("request_id")
        existing = _cached_idempotent_response(self.db, _CREATE_OPERATION, request_id)
        if existing is not None:
            return existing
        event_id = _event_id_for_request(request_id)
        uid = _uid_for_event_id(event_id)
        _reserve_create_request(self.db, request_id, event_id)
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
            return write_exception_status(exc, self.settings)
        result = create_calendar_event(
            self.db,
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
        _audit_result(self.db, _CREATE_OPERATION, result["event_id"], result)
        return result

    def update_event(self, input_data: dict[str, Any]) -> dict:
        """Update a full non-recurring event or recurring series."""

        event_id = input_data.get("event_id")
        patch = input_data.get("patch") or {}
        if not event_id:
            return _invalid_status(["event_id is required"])
        current = get_calendar_object(self.db, event_id)
        if not current:
            return not_found("event_id", event_id)
        errors = validate_event_patch(patch, current)
        if errors:
            return _invalid_status(errors)
        scope = input_data.get("scope", "series")
        if scope != "series":
            return _unsupported_scope_status(scope)
        credentials = load_icloud_credentials(self.settings)
        if not credentials:
            return _credential_missing_status()
        if str(current["href"]).startswith("local://"):
            return _event_sync_required_status()
        expected_etag = input_data.get("etag") or current.get("etag")
        if not expected_etag:
            return _missing_etag_conflict_status(event_id, current)

        raw_ics = patched_ics(current, patch)
        try:
            remote = CalDAVCalendarAdapter().update_event(
                apple_id=credentials.apple_id,
                app_password=credentials.app_password,
                event_href=current["href"],
                raw_ics=raw_ics,
                expected_etag=expected_etag,
            )
        except Exception as exc:
            return write_exception_status(exc, self.settings)
        if isinstance(remote, dict):
            return remote
        result = update_calendar_event(
            self.db,
            event_id=event_id,
            patch=patch,
            etag=input_data.get("etag"),
            scope=scope,
            etag_override=remote.etag,
            raw_ics_override=remote.raw_ics,
        )
        _audit_result(self.db, _UPDATE_OPERATION, event_id, result)
        return result


def _invalid_status(errors: list[str]) -> dict:
    return {"status": "invalid", "errors": errors}


def _credential_missing_status() -> dict:
    return {"status": "credential_missing", "message": "Configure ICLOUD_APPLE_ID and ICLOUD_APP_PASSWORD"}


def _calendar_sync_required_status() -> dict:
    return {"status": "sync_required", "message": "No writable remote CalDAV calendar is known"}


def _event_sync_required_status() -> dict:
    return {"status": "sync_required", "message": "Event has no remote CalDAV href. Sync calendar first."}


def _unsupported_scope_status(scope: str) -> dict:
    return {
        "status": "unsupported_scope",
        "supported_scopes": ["series"],
        "requested_scope": scope,
        "message": "Remote CalDAV scoped occurrence updates are not supported.",
    }


def _missing_etag_conflict_status(event_id: str, current: dict) -> dict:
    return {
        "status": "conflict",
        "event_id": event_id,
        "message": "Missing ETag; sync event before updating.",
        "latest_etag": None,
        "latest": {"title": current.get("summary"), "start": current.get("dtstart"), "end": current.get("dtend")},
    }


def _cached_idempotent_response(db: Database, operation: str, request_id: str | None) -> dict | None:
    if not request_id:
        return None
    existing = db.query_one(
        """
        SELECT response_json
        FROM idempotency_keys
        WHERE request_id = ? AND operation = ? AND response_json != ''
        """,
        (request_id, operation),
    )
    if existing:
        return parse_json(existing["response_json"], {})
    return None


def _event_id_for_request(request_id: str | None) -> str:
    return f"cal_evt_{uuid.uuid5(uuid.NAMESPACE_URL, request_id).hex}" if request_id else f"cal_evt_{uuid.uuid4().hex}"


def _uid_for_event_id(event_id: str) -> str:
    return f"{event_id}@icloud-mcp.local"


def _reserve_create_request(db: Database, request_id: str | None, event_id: str) -> None:
    if not request_id:
        return
    db.execute(
        """
        INSERT OR IGNORE INTO idempotency_keys (request_id, operation, object_id, response_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (request_id, _CREATE_OPERATION, event_id, "", utc_now()),
    )


def _audit_result(db: Database, operation: str, object_id: str, result: dict) -> None:
    audit_calendar_write(db, operation, object_id, result["status"])


def calendar_for_write(
    db: Database,
    settings: Settings,
    adapter: CalDAVCalendarAdapter,
    calendar_id: str | None,
) -> dict | None:
    """Return a writable remote Calendar collection, discovering if needed."""

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


def patched_ics(current: dict, patch: dict) -> str:
    """Patch stored ICS for remote update submission."""

    return patch_ics(current["raw_ics"], patch, current)


def write_exception_status(exc: Exception, settings: Settings) -> dict:
    """Return deterministic public Calendar write error details."""

    message = redact_text(str(exc), allow_unredacted=settings.allow_unredacted_debug) or exc.__class__.__name__
    lowered = message.casefold()
    if any(term in lowered for term in ["auth", "credential", "password", "unauthorized", "forbidden"]):
        return {"status": "credential_revoked_or_expired", "message": message}
    return {"status": "connectivity_error", "message": message, "queued": False}
