"""Calendar repository interface."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr
from icalendar import Alarm, Calendar, Event

from icloud_mcp.db.cache_state import bump_index_generation
from icloud_mcp.db.connection import Database
from icloud_mcp.db.search_repository import upsert_search_document
from icloud_mcp.util import compact_json, next_cursor, parse_json, sha256_text, utc_now


def list_calendars(db: Database) -> list[dict[str, Any]]:
    """Return known calendar collections."""

    rows = db.query(
        """
        SELECT id, display_name, read_only, color, last_sync_at
        FROM calendar_collections
        WHERE display_name IS NOT NULL
        ORDER BY display_name
        """
    )
    return [
        {
            "id": row["id"],
            "name": row["display_name"],
            "read_only": bool(row["read_only"]),
            "color": row["color"],
            "last_sync_at": row["last_sync_at"],
        }
        for row in rows
    ]


def get_calendar_collection(db: Database, calendar_id: str) -> dict[str, Any] | None:
    """Return one calendar collection."""

    return db.query_one("SELECT * FROM calendar_collections WHERE id = ?", (calendar_id,))


def first_writable_calendar(db: Database) -> dict[str, Any] | None:
    """Return first known writable remote calendar."""

    return db.query_one(
        """
        SELECT * FROM calendar_collections
        WHERE read_only = 0 AND url NOT LIKE 'local://%'
        ORDER BY display_name
        LIMIT 1
        """
    )


def upsert_calendar_collection(
    db: Database,
    *,
    account_id: str,
    calendar_id: str,
    url: str,
    display_name: str,
    color: str | None = None,
    sync_token: str | None = None,
    ctag: str | None = None,
    read_only: bool = False,
    last_sync_at: str | None = None,
) -> None:
    """Upsert a CalDAV calendar collection."""

    db.execute(
        """
        INSERT INTO calendar_collections
          (id, account_id, url, display_name, color, sync_token, ctag, read_only, last_sync_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
          id = excluded.id,
          display_name = excluded.display_name,
          color = COALESCE(excluded.color, calendar_collections.color),
          sync_token = COALESCE(excluded.sync_token, calendar_collections.sync_token),
          ctag = COALESCE(excluded.ctag, calendar_collections.ctag),
          read_only = excluded.read_only,
          last_sync_at = COALESCE(excluded.last_sync_at, calendar_collections.last_sync_at)
        """,
        (calendar_id, account_id, url, display_name, color, sync_token, ctag, 1 if read_only else 0, last_sync_at),
    )


def upsert_calendar_object(
    db: Database,
    *,
    calendar_id: str,
    event_id: str,
    href: str,
    uid: str,
    etag: str | None,
    raw_ics: str,
    summary: str,
    description: str | None,
    location: str | None,
    dtstart: str,
    dtend: str,
    timezone: str,
    attendees: list[dict[str, str]] | None = None,
    organizer: dict[str, str] | None = None,
    rrule: str | None = None,
    recurrence_id: str | None = None,
    status: str | None = None,
) -> None:
    """Upsert a CalDAV event and its searchable occurrence."""

    now = utc_now()
    db.execute(
        """
        INSERT INTO calendar_objects
          (id, calendar_id, href, uid, etag, raw_ics, summary, description, location, dtstart, dtend,
           timezone, rrule, recurrence_id, status, organizer_json, attendees_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(calendar_id, href) DO UPDATE SET
          id = excluded.id,
          uid = excluded.uid,
          etag = excluded.etag,
          raw_ics = excluded.raw_ics,
          summary = excluded.summary,
          description = excluded.description,
          location = excluded.location,
          dtstart = excluded.dtstart,
          dtend = excluded.dtend,
          timezone = excluded.timezone,
          rrule = excluded.rrule,
          recurrence_id = excluded.recurrence_id,
          status = excluded.status,
          organizer_json = excluded.organizer_json,
          attendees_json = excluded.attendees_json,
          updated_at = excluded.updated_at,
          deleted_at = NULL
        """,
        (
            event_id,
            calendar_id,
            href,
            uid,
            etag,
            raw_ics,
            summary,
            description,
            location,
            dtstart,
            dtend,
            timezone,
            rrule,
            recurrence_id,
            status,
            compact_json(organizer or {}),
            compact_json(attendees or []),
            now,
        ),
    )
    _replace_calendar_occurrences(
        db,
        event_id=event_id,
        dtstart=dtstart,
        dtend=dtend,
        timezone=timezone,
        rrule=rrule,
        recurrence_id=recurrence_id,
        status=status,
        raw_ics=raw_ics,
    )
    index_calendar_event(db, event_id)


def list_events(
    db: Database,
    *,
    calendar_ids: list[str] | None,
    start: str | None,
    end: str | None,
    limit: int,
    offset: int,
    cursor_secret: str,
) -> dict[str, Any]:
    """List events by occurrence time."""

    filters = ["o.deleted_at IS NULL"]
    parameters: list[Any] = []
    if calendar_ids:
        filters.append(f"o.calendar_id IN ({','.join('?' for _ in calendar_ids)})")
        parameters.extend(calendar_ids)
    if start:
        filters.append("co.occurrence_end >= ?")
        parameters.append(start)
    if end:
        filters.append("co.occurrence_start <= ?")
        parameters.append(end)

    rows = db.query(
        f"""
        SELECT
          o.id,
          o.calendar_id,
          o.summary,
          o.description,
          o.location,
          o.timezone,
          o.attendees_json,
          o.etag,
          co.occurrence_start,
          co.occurrence_end
        FROM calendar_occurrences co
        JOIN calendar_objects o ON o.id = co.event_id
        WHERE {" AND ".join(filters)}
        ORDER BY co.occurrence_start
        LIMIT ? OFFSET ?
        """,
        (*parameters, limit + 1, offset),
    )
    has_more = len(rows) > limit
    events = [_calendar_summary(row) for row in rows[:limit]]
    return {"events": events, "next_cursor": next_cursor(offset, len(events), limit, cursor_secret, has_more=has_more)}


def view_event(db: Database, event_id: str, include_raw_ics: bool) -> dict[str, Any] | None:
    """Return one event."""

    row = db.query_one("SELECT * FROM calendar_objects WHERE id = ? AND deleted_at IS NULL", (event_id,))
    if not row:
        return None
    event = _calendar_summary(row)
    event["description"] = row["description"]
    event["etag"] = row["etag"]
    event["content_trust"] = "untrusted_user_data"
    if include_raw_ics:
        event["raw_ics"] = row["raw_ics"]
    return event


def tombstone_calendar_object(db: Database, event_id: str) -> None:
    """Mark an event deleted and tombstone related occurrence/search rows."""

    now = utc_now()
    db.execute("UPDATE calendar_objects SET deleted_at = ? WHERE id = ?", (now, event_id))
    db.execute("DELETE FROM calendar_occurrences WHERE event_id = ?", (event_id,))
    db.execute(
        "UPDATE search_documents SET deleted_at = ? WHERE object_id = ? AND domain = 'calendar'", (now, event_id)
    )
    db.execute("DELETE FROM search_fts WHERE object_id = ? AND domain = 'calendar'", (event_id,))
    bump_index_generation(db)


def get_calendar_object(db: Database, event_id: str) -> dict[str, Any] | None:
    """Return raw calendar object row."""

    return db.query_one("SELECT * FROM calendar_objects WHERE id = ? AND deleted_at IS NULL", (event_id,))


def create_calendar_event(
    db: Database,
    *,
    calendar_id: str,
    title: str,
    start: str,
    end: str,
    timezone: str,
    location: str | None = None,
    description: str | None = None,
    attendees: list[dict[str, str]] | None = None,
    recurrence: dict[str, Any] | None = None,
    alarms: list[dict[str, Any]] | None = None,
    request_id: str | None = None,
    href: str | None = None,
    uid: str | None = None,
    etag: str | None = None,
    raw_ics: str | None = None,
    remote_state: str = "local_cached",
) -> dict[str, Any]:
    """Create a local cached calendar event with write-safe semantics."""

    if request_id:
        cached = db.query_one("SELECT response_json FROM idempotency_keys WHERE request_id = ?", (request_id,))
        if cached and cached["response_json"]:
            return parse_json(cached["response_json"], {})

    event_id = (
        f"cal_evt_{uuid.uuid5(uuid.NAMESPACE_URL, request_id).hex}" if request_id else f"cal_evt_{uuid.uuid4().hex}"
    )
    event_uid = uid or f"{event_id}@icloud-mcp.local"
    event_href = href or f"local://calendar/{calendar_id}/{event_id}.ics"
    event_etag = etag or "local-1"
    attendees_json = compact_json(attendees or [])
    event_ics = raw_ics or build_ics(
        uid=event_uid,
        title=title,
        start=start,
        end=end,
        timezone=timezone,
        location=location,
        description=description,
        attendees=attendees or [],
        recurrence=recurrence,
        alarms=alarms or [],
    )
    now = utc_now()
    if request_id:
        db.execute(
            """
            INSERT OR IGNORE INTO idempotency_keys (request_id, operation, object_id, response_json, created_at)
            VALUES (?, 'calendar.create_event', ?, '', ?)
            """,
            (request_id, event_id, now),
        )
    db.execute(
        """
        INSERT INTO calendar_objects
          (id, calendar_id, href, uid, etag, raw_ics, summary, description, location, dtstart, dtend,
           timezone, rrule, attendees_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            calendar_id,
            event_href,
            event_uid,
            event_etag,
            event_ics,
            title,
            description,
            location,
            start,
            end,
            timezone,
            compact_json(recurrence) if recurrence else None,
            attendees_json,
            now,
        ),
    )
    _replace_calendar_occurrences(
        db,
        event_id=event_id,
        dtstart=start,
        dtend=end,
        timezone=timezone,
        rrule=compact_json(recurrence) if recurrence else None,
        recurrence_id=None,
        status=None,
        raw_ics=event_ics,
    )
    index_calendar_event(db, event_id)
    response = {
        "status": "created",
        "event_id": event_id,
        "calendar_id": calendar_id,
        "etag": event_etag,
        "summary": {"title": title, "start": start, "end": end, "timezone": timezone},
        "remote": {"state": remote_state, "href": event_href},
    }
    if request_id:
        db.execute(
            """
            INSERT INTO idempotency_keys (request_id, operation, object_id, response_json, created_at)
            VALUES (?, 'calendar.create_event', ?, ?, ?)
            ON CONFLICT(request_id) DO UPDATE SET
              object_id = excluded.object_id,
              response_json = excluded.response_json
            """,
            (request_id, event_id, compact_json(response), now),
        )
    return response


def update_calendar_event(
    db: Database,
    *,
    event_id: str,
    patch: dict[str, Any],
    etag: str | None,
    scope: str,
    etag_override: str | None = None,
    raw_ics_override: str | None = None,
) -> dict[str, Any]:
    """Update a non-recurring event or full series with ETag conflict checks."""

    current = db.query_one("SELECT * FROM calendar_objects WHERE id = ? AND deleted_at IS NULL", (event_id,))
    if not current:
        return {"status": "not_found", "event_id": event_id}
    if etag and etag != current["etag"]:
        return {
            "status": "conflict",
            "event_id": event_id,
            "provided_etag": etag,
            "latest_etag": current["etag"],
            "latest": _calendar_summary(current),
        }
    errors = validate_event_patch(patch, current)
    if errors:
        return {"status": "invalid", "event_id": event_id, "errors": errors}
    if scope == "single":
        return _update_single_occurrence(
            db, current, patch, etag_override=etag_override, raw_ics_override=raw_ics_override
        )
    if scope == "future":
        return _update_future_occurrences(
            db, current, patch, etag_override=etag_override, raw_ics_override=raw_ics_override
        )
    if scope not in {"series", "future"}:
        return {
            "status": "unsupported_scope",
            "supported_scopes": ["single", "future", "series"],
            "requested_scope": scope,
        }

    title = patch.get("title", current["summary"])
    start = patch.get("start", current["dtstart"])
    end = patch.get("end", current["dtend"])
    timezone = patch.get("timezone", current["timezone"])
    location = patch.get("location", current["location"])
    description = patch.get("description", current["description"])
    attendees = patch.get("attendees", parse_json(current["attendees_json"], []))
    recurrence = patch.get("recurrence", _stored_rrule_to_recurrence(current["rrule"]))

    raw_ics = raw_ics_override or patch_ics(current["raw_ics"], patch, current)
    previous_etag = current["etag"] or "local-1"
    revision = int(previous_etag.rsplit("-", 1)[-1]) + 1 if previous_etag.startswith("local-") else 2
    new_etag = etag_override or f"local-{revision}"
    now = utc_now()
    db.execute(
        """
        UPDATE calendar_objects
        SET etag = ?, raw_ics = ?, summary = ?, description = ?, location = ?, dtstart = ?, dtend = ?,
            timezone = ?, rrule = ?, attendees_json = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            new_etag,
            raw_ics,
            title,
            description,
            location,
            start,
            end,
            timezone,
            compact_json(recurrence) if recurrence else None,
            compact_json(attendees),
            now,
            event_id,
        ),
    )
    _replace_calendar_occurrences(
        db,
        event_id=event_id,
        dtstart=start,
        dtend=end,
        timezone=timezone,
        rrule=compact_json(recurrence) if recurrence else None,
        recurrence_id=current["recurrence_id"],
        status=current["status"],
        raw_ics=raw_ics,
    )
    index_calendar_event(db, event_id)
    return {
        "status": "updated",
        "event_id": event_id,
        "etag": new_etag,
        "diff": sorted(patch.keys()),
        "scope": scope,
        "summary": {"title": title, "start": start, "end": end, "timezone": timezone},
    }


def _update_future_occurrences(
    db: Database,
    current: dict[str, Any],
    patch: dict[str, Any],
    *,
    etag_override: str | None,
    raw_ics_override: str | None,
) -> dict[str, Any]:
    cutoff = patch.get("occurrence_start") or patch.get("start") or current["dtstart"]
    timezone = patch.get("timezone", current["timezone"])
    cutoff_dt = _datetime_value(str(cutoff), timezone)
    current_start = _datetime_value(current["dtstart"], current["timezone"])
    current_end = _datetime_value(current["dtend"], current["timezone"])
    duration = current_end - current_start
    if duration <= timedelta(0):
        duration = timedelta(hours=1)
    if cutoff_dt <= current_start:
        series_patch = {key: value for key, value in patch.items() if key not in {"occurrence_start", "recurrence_id"}}
        return update_calendar_event(
            db,
            event_id=current["id"],
            patch=series_patch,
            etag=current["etag"],
            scope="series",
            etag_override=etag_override,
            raw_ics_override=raw_ics_override,
        )

    original_recurrence = _stored_rrule_to_recurrence(current["rrule"]) or {}
    until_dt = cutoff_dt.astimezone(UTC) - timedelta(seconds=1)
    truncated_recurrence = {
        key: value for key, value in original_recurrence.items() if key.casefold() not in {"count", "until"}
    }
    truncated_recurrence["until"] = until_dt.strftime("%Y%m%dT%H%M%SZ")
    now = utc_now()
    recurrence_for_ics = {**truncated_recurrence, "until": until_dt}
    original_raw_ics = patch_ics(current["raw_ics"], {"recurrence": recurrence_for_ics}, current)
    db.execute(
        """
        UPDATE calendar_objects
        SET rrule = ?, raw_ics = ?, updated_at = ?
        WHERE id = ?
        """,
        (compact_json(truncated_recurrence), original_raw_ics, now, current["id"]),
    )
    _replace_calendar_occurrences(
        db,
        event_id=current["id"],
        dtstart=current["dtstart"],
        dtend=current["dtend"],
        timezone=current["timezone"],
        rrule=compact_json(truncated_recurrence),
        recurrence_id=current["recurrence_id"],
        status=current["status"],
        raw_ics=original_raw_ics,
    )
    index_calendar_event(db, current["id"])

    future_start = patch.get("start") or cutoff_dt.isoformat()
    future_end = patch.get("end") or (_datetime_value(str(future_start), timezone) + duration).isoformat()
    future_title = patch.get("title", current["summary"])
    future_recurrence = patch.get("recurrence", original_recurrence) or None
    future_event_id = f"{current['id']}_future_{sha256_text(str(cutoff))[:12]}"
    future_raw_ics = raw_ics_override or build_ics(
        uid=current["uid"],
        title=future_title,
        start=future_start,
        end=future_end,
        timezone=timezone,
        location=patch.get("location", current["location"]),
        description=patch.get("description", current["description"]),
        attendees=patch.get("attendees", parse_json(current["attendees_json"], [])),
        recurrence=future_recurrence,
        alarms=[],
    )
    upsert_calendar_object(
        db,
        calendar_id=current["calendar_id"],
        event_id=future_event_id,
        href=f"{current['href']}#future-{sha256_text(str(cutoff))[:12]}",
        uid=current["uid"],
        etag=etag_override or current["etag"],
        raw_ics=future_raw_ics,
        summary=future_title,
        description=patch.get("description", current["description"]),
        location=patch.get("location", current["location"]),
        dtstart=future_start,
        dtend=future_end,
        timezone=timezone,
        attendees=patch.get("attendees", parse_json(current["attendees_json"], [])),
        organizer=parse_json(current["organizer_json"], {}),
        rrule=compact_json(future_recurrence) if future_recurrence else None,
        recurrence_id=None,
        status=patch.get("status", current["status"]),
    )
    return {
        "status": "updated",
        "event_id": future_event_id,
        "previous_event_id": current["id"],
        "etag": etag_override or current["etag"],
        "scope": "future",
        "split_at": cutoff_dt.isoformat(),
        "diff": sorted(patch.keys()),
        "summary": {"title": future_title, "start": future_start, "end": future_end, "timezone": timezone},
    }


def _update_single_occurrence(
    db: Database,
    current: dict[str, Any],
    patch: dict[str, Any],
    *,
    etag_override: str | None,
    raw_ics_override: str | None,
) -> dict[str, Any]:
    recurrence_id = patch.get("recurrence_id") or patch.get("occurrence_start") or current["dtstart"]
    title = patch.get("title", current["summary"])
    start = patch.get("start", recurrence_id)
    end = patch.get("end", current["dtend"])
    timezone = patch.get("timezone", current["timezone"])
    event_id = f"{current['id']}_detached_{sha256_text(str(recurrence_id))[:12]}"
    raw_ics = raw_ics_override or build_ics(
        uid=current["uid"],
        title=title,
        start=start,
        end=end,
        timezone=timezone,
        location=patch.get("location", current["location"]),
        description=patch.get("description", current["description"]),
        attendees=patch.get("attendees", parse_json(current["attendees_json"], [])),
        recurrence=None,
        alarms=[],
    )
    upsert_calendar_object(
        db,
        calendar_id=current["calendar_id"],
        event_id=event_id,
        href=f"{current['href']}#recurrence-{sha256_text(str(recurrence_id))[:12]}",
        uid=current["uid"],
        etag=etag_override or current["etag"],
        raw_ics=raw_ics,
        summary=title,
        description=patch.get("description", current["description"]),
        location=patch.get("location", current["location"]),
        dtstart=start,
        dtend=end,
        timezone=timezone,
        attendees=patch.get("attendees", parse_json(current["attendees_json"], [])),
        organizer=parse_json(current["organizer_json"], {}),
        rrule=None,
        recurrence_id=str(recurrence_id),
        status=patch.get("status", current["status"]),
    )
    return {
        "status": "updated",
        "event_id": event_id,
        "etag": etag_override or current["etag"],
        "scope": "single",
        "diff": sorted(patch.keys()),
        "summary": {"title": title, "start": start, "end": end, "timezone": timezone},
    }


def _replace_calendar_occurrences(
    db: Database,
    *,
    event_id: str,
    dtstart: str,
    dtend: str,
    timezone: str,
    rrule: str | None,
    recurrence_id: str | None,
    status: str | None,
    raw_ics: str | None = None,
) -> None:
    db.execute("DELETE FROM calendar_occurrences WHERE event_id = ?", (event_id,))
    rows = [
        (
            _calendar_occurrence_id(event_id, occurrence_start, recurrence_id),
            event_id,
            occurrence_start,
            occurrence_end,
            recurrence_id,
            1 if status == "CANCELLED" else 0,
        )
        for occurrence_start, occurrence_end in _calendar_occurrence_windows(dtstart, dtend, timezone, rrule, raw_ics)
    ]
    db.executemany(
        """
        INSERT INTO calendar_occurrences (id, event_id, occurrence_start, occurrence_end, recurrence_id, is_cancelled)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def _calendar_occurrence_id(event_id: str, occurrence_start: str, recurrence_id: str | None) -> str:
    source = f"{event_id}:{occurrence_start}:{recurrence_id or ''}"
    return f"cal_occ_{sha256_text(source)[:24]}"


def _calendar_occurrence_windows(
    dtstart: str,
    dtend: str,
    timezone: str,
    rrule: str | None,
    raw_ics: str | None = None,
) -> list[tuple[str, str]]:
    start_dt = _datetime_value(dtstart, timezone)
    end_dt = _datetime_value(dtend, timezone)
    duration = end_dt - start_dt
    if duration <= timedelta(0):
        duration = timedelta(hours=1)

    recurrence_rule = _rrule_text(rrule)
    if not recurrence_rule:
        return _non_recurring_windows(dtstart, dtend, raw_ics)

    try:
        rule = rrulestr(recurrence_rule, dtstart=start_dt)
        horizon = start_dt + timedelta(days=366 * 5)
        starts = list(rule.between(start_dt - timedelta(seconds=1), horizon, inc=True))[:730]
    except (TypeError, ValueError):
        return [(dtstart, dtend)]

    exdates, rdates, cancelled = _ics_recurrence_exceptions(raw_ics, timezone)
    start_by_key = {_occurrence_key(start): start for start in starts}
    for exdate in exdates | cancelled:
        start_by_key.pop(_occurrence_key(exdate), None)
    for rdate in rdates:
        start_by_key[_occurrence_key(rdate)] = rdate
    starts = sorted(start_by_key.values())
    return [(start.isoformat(), (start + duration).isoformat()) for start in starts] or [(dtstart, dtend)]


def _rrule_text(rrule: str | None) -> str | None:
    if not rrule:
        return None
    if rrule.startswith("RRULE:") or "\nRRULE:" in rrule:
        return rrule
    if rrule.startswith("{"):
        recurrence = _stored_rrule_to_recurrence(rrule) or {}
        parts = [f"{key.upper()}={str(value).upper()}" for key, value in recurrence.items() if value is not None]
        return ";".join(parts)
    return rrule


def _non_recurring_windows(dtstart: str, dtend: str, raw_ics: str | None) -> list[tuple[str, str]]:
    exdates, rdates, cancelled = _ics_recurrence_exceptions(raw_ics, "UTC")
    if _occurrence_key(_datetime_value(dtstart, "UTC")) in {_occurrence_key(value) for value in exdates | cancelled}:
        return []
    windows = [(dtstart, dtend)]
    base_start = _datetime_value(dtstart, "UTC")
    base_end = _datetime_value(dtend, "UTC")
    duration = base_end - base_start
    for rdate in sorted(rdates):
        windows.append((rdate.isoformat(), (rdate + duration).isoformat()))
    return windows


def _ics_recurrence_exceptions(
    raw_ics: str | None, timezone: str
) -> tuple[set[datetime], set[datetime], set[datetime]]:
    if not raw_ics:
        return set(), set(), set()
    try:
        calendar = Calendar.from_ical(raw_ics)
    except ValueError:
        return set(), set(), set()
    exdates: set[datetime] = set()
    rdates: set[datetime] = set()
    cancelled: set[datetime] = set()
    for component in calendar.walk():
        if component.name != "VEVENT":
            continue
        for value in _as_ical_list(component.get("EXDATE")):
            exdates.update(_date_list_values(value, timezone))
        for value in _as_ical_list(component.get("RDATE")):
            rdates.update(_date_list_values(value, timezone))
        if str(component.get("STATUS", "")).upper() == "CANCELLED" and component.get("RECURRENCE-ID"):
            cancelled.add(_ical_datetime(component.get("RECURRENCE-ID").dt, timezone))
    return exdates, rdates, cancelled


def _as_ical_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _date_list_values(value: Any, timezone: str) -> set[datetime]:
    dates = getattr(value, "dts", None)
    if dates is None:
        return {_ical_datetime(getattr(value, "dt", value), timezone)}
    return {_ical_datetime(item.dt, timezone) for item in dates}


def _ical_datetime(value: Any, timezone: str) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=ZoneInfo(timezone))
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time(), tzinfo=ZoneInfo(timezone))
    return _datetime_value(str(value), timezone)


def _occurrence_key(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0).isoformat()


def _stored_rrule_to_recurrence(rrule: str | None) -> dict[str, Any] | None:
    if not rrule:
        return None
    if rrule.startswith("{"):
        return parse_json(rrule, None)

    value = rrule.removeprefix("RRULE:")
    recurrence: dict[str, Any] = {}
    for part in value.split(";"):
        if "=" not in part:
            continue
        key, raw_value = part.split("=", 1)
        normalized_value: Any = raw_value
        if raw_value.isdigit():
            normalized_value = int(raw_value)
        recurrence[key.lower()] = normalized_value
    return recurrence or None


def index_calendar_event(db: Database, event_id: str) -> None:
    """Build compact calendar search document."""

    row = db.query_one("SELECT * FROM calendar_objects WHERE id = ?", (event_id,))
    if not row:
        return
    attendees = parse_json(row["attendees_json"], [])
    people = [attendee.get("name") or attendee.get("email", "") for attendee in attendees]
    text = "\n".join(
        part
        for part in [
            f"Title: {row['summary']}",
            f"Start: {row['dtstart']}",
            f"End: {row['dtend']}",
            f"Attendees: {', '.join(people)}",
            f"Location: {row['location'] or ''}",
            row["description"] or "",
        ]
        if part
    )
    upsert_search_document(
        db,
        document_id=f"doc_{event_id}",
        domain="calendar",
        object_id=event_id,
        title=row["summary"] or "",
        text=text,
        metadata={
            "time": {"start": row["dtstart"], "end": row["dtend"], "timezone": row["timezone"]},
            "participants": people[:5],
        },
        sender="",
        participants=" ".join(people),
    )
    db.execute(
        "UPDATE search_documents SET deleted_at = ? WHERE id LIKE ? AND id != ?",
        (utc_now(), f"doc_{event_id}_occ_%", f"doc_{event_id}"),
    )
    occurrences = db.query(
        """
        SELECT id, occurrence_start, occurrence_end, recurrence_id, is_cancelled
        FROM calendar_occurrences
        WHERE event_id = ? AND is_cancelled = 0
        ORDER BY occurrence_start
        LIMIT 730
        """,
        (event_id,),
    )
    for occurrence in occurrences:
        occurrence_text = "\n".join(
            part
            for part in [
                f"Title: {row['summary']}",
                f"Start: {occurrence['occurrence_start']}",
                f"End: {occurrence['occurrence_end']}",
                f"Attendees: {', '.join(people)}",
                f"Location: {row['location'] or ''}",
            ]
            if part
        )
        upsert_search_document(
            db,
            document_id=f"doc_{event_id}_occ_{occurrence['id']}",
            domain="calendar",
            object_id=event_id,
            occurrence_id=occurrence["id"],
            title=row["summary"] or "",
            text=occurrence_text,
            metadata={
                "time": {
                    "start": occurrence["occurrence_start"],
                    "end": occurrence["occurrence_end"],
                    "timezone": row["timezone"],
                },
                "participants": people[:5],
            },
            participants=" ".join(people),
            chunks=[{"type": "occurrence", "text": occurrence_text}],
        )


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


def _valid_iana_timezone(timezone: str) -> bool:
    try:
        ZoneInfo(timezone)
    except Exception:
        return False
    return True


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


def build_ics(
    *,
    uid: str,
    title: str,
    start: str,
    end: str,
    timezone: str,
    location: str | None,
    description: str | None,
    attendees: list[dict[str, str]],
    recurrence: dict[str, Any] | None,
    alarms: list[dict[str, Any]],
) -> str:
    """Generate a valid VEVENT for cached local and CalDAV writes."""

    calendar = Calendar()
    calendar.add("prodid", "-//icloud-mcp//EN")
    calendar.add("version", "2.0")
    calendar.add("X-WR-TIMEZONE", timezone)

    event = Event()
    event.add("uid", uid)
    event.add("summary", title)
    event.add("dtstart", _ics_temporal_value(start, timezone))
    event.add("dtend", _ics_temporal_value(end, timezone))
    if location:
        event.add("location", location)
    if description:
        event.add("description", description)
    for attendee in attendees:
        email = attendee.get("email", "")
        name = attendee.get("name", email)
        event.add("attendee", f"mailto:{email}", parameters={"CN": name})
    if recurrence:
        event.add("rrule", {key.upper(): value for key, value in recurrence.items()})
    for alarm in alarms:
        minutes = int(alarm.get("minutes_before", 0))
        if minutes > 0:
            valarm = Alarm()
            valarm.add("action", "DISPLAY")
            valarm.add("trigger", -timedelta(minutes=minutes))
            event.add_component(valarm)
    calendar.add_component(event)
    return calendar.to_ical().decode("utf-8")


def patch_ics(raw_ics: str, patch: dict[str, Any], current: dict[str, Any]) -> str:
    """Patch known VEVENT fields while preserving unknown ICS properties."""

    try:
        calendar = Calendar.from_ical(raw_ics)
    except ValueError:
        calendar = None
    if calendar is None:
        return build_ics(
            uid=current["uid"],
            title=patch.get("title", current["summary"]),
            start=patch.get("start", current["dtstart"]),
            end=patch.get("end", current["dtend"]),
            timezone=patch.get("timezone", current["timezone"]),
            location=patch.get("location", current["location"]),
            description=patch.get("description", current["description"]),
            attendees=patch.get("attendees", parse_json(current.get("attendees_json"), [])),
            recurrence=patch.get("recurrence", _stored_rrule_to_recurrence(current.get("rrule"))),
            alarms=[],
        )
    event = next((item for item in calendar.walk() if item.name == "VEVENT" and not item.get("RECURRENCE-ID")), None)
    if event is None:
        return raw_ics
    _replace_ics_property(event, "SUMMARY", patch.get("title"))
    _replace_ics_property(event, "LOCATION", patch.get("location"))
    _replace_ics_property(event, "DESCRIPTION", patch.get("description"))
    if patch.get("start"):
        _replace_ics_property(
            event, "DTSTART", _ics_temporal_value(patch["start"], patch.get("timezone", current["timezone"]))
        )
    if patch.get("end"):
        _replace_ics_property(
            event, "DTEND", _ics_temporal_value(patch["end"], patch.get("timezone", current["timezone"]))
        )
    if "recurrence" in patch:
        if "RRULE" in event:
            del event["RRULE"]
        if patch["recurrence"]:
            event.add("rrule", {key.upper(): value for key, value in patch["recurrence"].items()})
    if "attendees" in patch:
        while "ATTENDEE" in event:
            del event["ATTENDEE"]
        for attendee in patch.get("attendees") or []:
            email = attendee.get("email", "")
            event.add("attendee", f"mailto:{email}", parameters={"CN": attendee.get("name", email)})
    return calendar.to_ical().decode("utf-8")


def _replace_ics_property(event: Event, name: str, value: Any) -> None:
    if value is None:
        return
    if name in event:
        del event[name]
    event.add(name.lower(), value)


def _ics_temporal_value(value: str, timezone: str) -> datetime | date:
    if "T" not in value:
        return date.fromisoformat(value)

    parsed = datetime.fromisoformat(value)
    named_timezone = ZoneInfo(timezone)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=named_timezone)
    return parsed.astimezone(named_timezone)


def _datetime_value(value: str, timezone: str) -> datetime:
    named_timezone = ZoneInfo(timezone)
    if "T" not in value:
        return datetime.combine(date.fromisoformat(value), datetime.min.time(), tzinfo=named_timezone)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=named_timezone)
    return parsed.astimezone(named_timezone)


def _calendar_summary(row: dict[str, Any]) -> dict[str, Any]:
    attendees = parse_json(row.get("attendees_json"), [])
    participants = [attendee.get("name") or attendee.get("email", "") for attendee in attendees]
    return {
        "id": row["id"],
        "calendar_id": row.get("calendar_id"),
        "title": row.get("summary"),
        "time": {
            "start": row.get("occurrence_start") or row.get("dtstart"),
            "end": row.get("occurrence_end") or row.get("dtend"),
            "timezone": row.get("timezone"),
        },
        "location": row.get("location"),
        "participants": participants[:5],
        "etag": row.get("etag"),
    }
