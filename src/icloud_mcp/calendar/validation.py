"""Calendar write validation."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from icloud_mcp.platform.util import parse_json


def validate_event_input(input_data: dict[str, Any]) -> list[str]:
    """Validate calendar write fields."""

    errors: list[str] = []
    title = str(input_data.get("title") or "")
    if not title.strip():
        errors.append("title is required")
    if len(title) > 200:
        errors.append("title must be 200 characters or fewer")

    start = input_data.get("start")
    end = input_data.get("end")
    timezone = input_data.get("timezone")
    if not start:
        errors.append("start is required")
    if not end:
        errors.append("end is required")
    if not timezone:
        errors.append("timezone is required")
    elif not _valid_iana_timezone(str(timezone)):
        errors.append("timezone must be a valid IANA timezone")
    if start and end:
        try:
            start_dt = datetime.fromisoformat(str(start))
            end_dt = datetime.fromisoformat(str(end))
            if end_dt <= start_dt:
                errors.append("end must be after start")
        except TypeError:
            errors.append("start and end must both include timezone offsets or both omit them")
        except ValueError:
            errors.append("start and end must be ISO 8601 datetimes")

    attendees = input_data.get("attendees") or []
    if not isinstance(attendees, list):
        errors.append("attendees must be a list")
    else:
        for attendee in attendees:
            email = attendee.get("email") if isinstance(attendee, dict) else None
            if email and "@" not in email:
                errors.append(f"invalid attendee email: {email}")

    recurrence = input_data.get("recurrence")
    if isinstance(recurrence, dict):
        count = recurrence.get("count")
        if isinstance(count, int) and count > 730:
            errors.append("recurrence count must be 730 or fewer")

    return errors


def validate_event_patch(patch: dict[str, Any], current: dict[str, Any] | None = None) -> list[str]:
    """Validate calendar update patch."""

    if not patch:
        return ["patch must not be empty"]
    current_attendees = parse_json(current.get("attendees_json"), []) if current else []
    patch_changes_time = "start" in patch or "end" in patch
    default_start = current.get("dtstart") if current and patch_changes_time else "2026-01-01T00:00:00+00:00"
    default_end = current.get("dtend") if current and patch_changes_time else "2026-01-01T01:00:00+00:00"
    input_data = {
        "title": patch.get("title", current.get("summary") if current else "existing event"),
        "start": patch.get("start", default_start),
        "end": patch.get("end", default_end),
        "timezone": patch.get("timezone", current.get("timezone") if current else "UTC"),
        "attendees": patch.get("attendees", current_attendees),
        "recurrence": patch.get("recurrence"),
    }
    return validate_event_input(input_data)


def _valid_iana_timezone(timezone: str) -> bool:
    try:
        ZoneInfo(timezone)
    except Exception:
        return False
    return True
