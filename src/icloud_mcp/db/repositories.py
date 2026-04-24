"""Repository functions for local cache and search index."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from icalendar import Alarm, Calendar, Event

from icloud_mcp.config import Settings
from icloud_mcp.db.connection import Database
from icloud_mcp.indexing.vector import cosine_score
from icloud_mcp.util import (
    compact_json,
    next_cursor,
    normalize_text,
    parse_json,
    sha256_text,
    tokenize,
    truncate,
    utc_now,
)


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
    return {
        "mail_last_sync": mail["value"] if mail else None,
        "calendar_last_sync": calendar["value"] if calendar else None,
        "contacts_last_sync": contacts["value"] if contacts else None,
    }


def upsert_search_document(
    db: Database,
    *,
    document_id: str,
    domain: str,
    object_id: str,
    title: str,
    text: str,
    metadata: dict[str, Any] | None = None,
    occurrence_id: str | None = None,
    sender: str = "",
    participants: str = "",
) -> None:
    """Upsert one search document, its first chunk, and FTS row."""

    now = utc_now()
    metadata_json = compact_json(metadata or {})
    text_hash = sha256_text(text)
    chunk_id = f"{document_id}:0"

    db.execute(
        """
        INSERT INTO search_documents
          (id, domain, object_id, occurrence_id, title, canonical_text, metadata_json, updated_at, deleted_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
        ON CONFLICT(id) DO UPDATE SET
          domain = excluded.domain,
          object_id = excluded.object_id,
          occurrence_id = excluded.occurrence_id,
          title = excluded.title,
          canonical_text = excluded.canonical_text,
          metadata_json = excluded.metadata_json,
          updated_at = excluded.updated_at,
          deleted_at = NULL
        """,
        (document_id, domain, object_id, occurrence_id, title, text, metadata_json, now),
    )
    db.execute(
        """
        INSERT INTO search_chunks
          (id, document_id, chunk_index, text, token_count, text_hash, metadata_json, updated_at)
        VALUES (?, ?, 0, ?, ?, ?, ?, ?)
        ON CONFLICT(document_id, chunk_index) DO UPDATE SET
          text = excluded.text,
          token_count = excluded.token_count,
          text_hash = excluded.text_hash,
          metadata_json = excluded.metadata_json,
          updated_at = excluded.updated_at
        """,
        (chunk_id, document_id, text, len(tokenize(text)), text_hash, metadata_json, now),
    )
    db.execute("DELETE FROM search_fts WHERE document_id = ?", (document_id,))
    db.execute(
        """
        INSERT INTO search_fts (document_id, object_id, domain, title, text, sender, participants)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (document_id, object_id, domain, title, text, sender, participants),
    )
    bump_index_generation(db)


def search_documents(
    db: Database,
    *,
    query: str,
    domains: list[str],
    limit: int,
    offset: int,
    snippet_chars: int,
) -> list[dict[str, Any]]:
    """Run local lexical FTS search and return compact rows."""

    terms = tokenize(query)
    if terms:
        fts_query = " OR ".join(f'"{term}"' for term in terms[:8])
        placeholders = ",".join("?" for _ in domains)
        rows = db.query(
            f"""
            SELECT
              d.id,
              d.domain,
              d.object_id,
              d.occurrence_id,
              d.title,
              d.canonical_text,
              d.metadata_json,
              bm25(search_fts) AS rank
            FROM search_fts
            JOIN search_documents d ON d.id = search_fts.document_id
            WHERE search_fts MATCH ?
              AND d.deleted_at IS NULL
              AND d.domain IN ({placeholders})
            ORDER BY rank ASC
            LIMIT ? OFFSET ?
            """,
            (fts_query, *domains, limit, offset),
        )
    else:
        placeholders = ",".join("?" for _ in domains)
        rows = db.query(
            f"""
            SELECT id, domain, object_id, occurrence_id, title, canonical_text, metadata_json, 0.0 AS rank
            FROM search_documents
            WHERE deleted_at IS NULL AND domain IN ({placeholders})
            ORDER BY updated_at DESC
            LIMIT ? OFFSET ?
            """,
            (*domains, limit, offset),
        )

    rows = _add_semantic_results(db, query=query, domains=domains, rows=rows, limit=limit)

    results = []
    for index, row in enumerate(rows[:limit]):
        metadata = parse_json(row.get("metadata_json"), {})
        score = row.get("score")
        if score is None:
            score = max(0.0, 1.0 - (offset + index) * 0.05)
        results.append(
            {
                "id": row["object_id"],
                "document_id": row["id"],
                "domain": "contacts" if row["domain"] == "contact" else row["domain"],
                "title": truncate(row.get("title"), 120),
                "snippet": truncate(row.get("canonical_text"), snippet_chars),
                "score": round(float(score), 3),
                "why": row.get("why", ["lexical_match"] if terms else ["recent_indexed_item"]),
                **metadata,
            }
        )
    return results


def _add_semantic_results(
    db: Database,
    *,
    query: str,
    domains: list[str],
    rows: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    existing = {row["id"] for row in rows}
    placeholders = ",".join("?" for _ in domains)
    candidates = db.query(
        f"""
        SELECT id, domain, object_id, occurrence_id, title, canonical_text, metadata_json
        FROM search_documents
        WHERE deleted_at IS NULL AND domain IN ({placeholders})
        """,
        (*domains,),
    )
    semantic_rows = []
    for candidate in candidates:
        if candidate["id"] in existing:
            continue
        score = cosine_score(query, candidate["canonical_text"])
        if score <= 0:
            continue
        candidate["score"] = score
        candidate["why"] = ["semantic_match"]
        semantic_rows.append(candidate)
    semantic_rows.sort(key=lambda row: row["score"], reverse=True)
    return rows + semantic_rows[: max(0, limit - len(rows))]


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
    db.execute("DELETE FROM calendar_occurrences WHERE event_id = ?", (event_id,))
    db.execute(
        """
        INSERT INTO calendar_occurrences (id, event_id, occurrence_start, occurrence_end, recurrence_id, is_cancelled)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            f"cal_occ_{uuid.uuid4().hex}",
            event_id,
            dtstart,
            dtend,
            recurrence_id,
            1 if status == "CANCELLED" else 0,
        ),
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
        (*parameters, limit, offset),
    )
    events = [_calendar_summary(row) for row in rows]
    return {"events": events, "next_cursor": next_cursor(offset, len(events), limit, cursor_secret)}


def view_event(db: Database, event_id: str, include_raw_ics: bool) -> dict[str, Any] | None:
    """Return one event."""

    row = db.query_one("SELECT * FROM calendar_objects WHERE id = ? AND deleted_at IS NULL", (event_id,))
    if not row:
        return None
    event = _calendar_summary(row)
    event["description"] = row["description"]
    event["etag"] = row["etag"]
    if include_raw_ics:
        event["raw_ics"] = row["raw_ics"]
    return event


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
        if cached:
            return parse_json(cached["response_json"], {})

    event_id = f"cal_evt_{uuid.uuid4().hex}"
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
    occurrence_id = f"cal_occ_{uuid.uuid4().hex}"
    db.execute(
        """
        INSERT INTO calendar_occurrences (id, event_id, occurrence_start, occurrence_end)
        VALUES (?, ?, ?, ?)
        """,
        (occurrence_id, event_id, start, end),
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
            "latest": _calendar_summary(current),
        }
    if scope != "series":
        return {"status": "unsupported_scope", "supported_scopes": ["series"], "requested_scope": scope}

    title = patch.get("title", current["summary"])
    start = patch.get("start", current["dtstart"])
    end = patch.get("end", current["dtend"])
    timezone = patch.get("timezone", current["timezone"])
    location = patch.get("location", current["location"])
    description = patch.get("description", current["description"])
    attendees = patch.get("attendees", parse_json(current["attendees_json"], []))
    recurrence = patch.get("recurrence", parse_json(current["rrule"], None))

    raw_ics = raw_ics_override or build_ics(
        uid=current["uid"],
        title=title,
        start=start,
        end=end,
        timezone=timezone,
        location=location,
        description=description,
        attendees=attendees,
        recurrence=recurrence,
        alarms=[],
    )
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
    db.execute(
        """
        UPDATE calendar_occurrences
        SET occurrence_start = ?, occurrence_end = ?
        WHERE event_id = ?
        """,
        (start, end, event_id),
    )
    index_calendar_event(db, event_id)
    return {
        "status": "updated",
        "event_id": event_id,
        "etag": new_etag,
        "diff": sorted(patch.keys()),
        "summary": {"title": title, "start": start, "end": end, "timezone": timezone},
    }


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


def list_mail(
    db: Database,
    *,
    mailbox: str,
    after: str | None,
    before: str | None,
    sender: str | None,
    limit: int,
    offset: int,
    cursor_secret: str,
) -> dict[str, Any]:
    """List compact mail rows."""

    filters = ["m.deleted_at IS NULL", "mb.name = ?"]
    parameters: list[Any] = [mailbox]
    if after:
        filters.append("m.date >= ?")
        parameters.append(after)
    if before:
        filters.append("m.date <= ?")
        parameters.append(before)
    if sender:
        filters.append("m.from_json LIKE ?")
        parameters.append(f"%{sender}%")

    rows = db.query(
        f"""
        SELECT m.*, mb.name AS mailbox_name
        FROM mail_messages m
        JOIN mailboxes mb ON mb.id = m.mailbox_id
        WHERE {" AND ".join(filters)}
        ORDER BY m.date DESC
        LIMIT ? OFFSET ?
        """,
        (*parameters, limit, offset),
    )
    messages = [
        {
            "id": row["id"],
            "mailbox": row["mailbox_name"],
            "subject": row["subject"],
            "from": parse_json(row["from_json"], {}),
            "date": row["date"],
            "preview": row["preview"],
            "has_attachments": bool(row["has_attachments"]),
        }
        for row in rows
    ]
    return {"messages": messages, "next_cursor": next_cursor(offset, len(messages), limit, cursor_secret)}


def view_mail(db: Database, message_id: str, include: list[str], max_body_chars: int) -> dict[str, Any] | None:
    """Return one mail message with optional compact body."""

    row = db.query_one("SELECT * FROM mail_messages WHERE id = ? AND deleted_at IS NULL", (message_id,))
    if not row:
        return None
    result: dict[str, Any] = {
        "id": row["id"],
        "subject": row["subject"],
        "date": row["date"],
    }
    if "headers" in include:
        result["headers"] = {
            "from": parse_json(row["from_json"], {}),
            "to": parse_json(row["to_json"], []),
            "cc": parse_json(row["cc_json"], []),
            "message_id": row["message_id"],
            "flags": parse_json(row["flags_json"], []),
        }
    if "body_text" in include:
        body = row["body_text"] or ""
        result["body_text"] = body[:max_body_chars]
        result["body_truncated"] = len(body) > max_body_chars
    if "attachments" in include:
        result["attachments"] = []
    return result


def upsert_mailbox(
    db: Database, *, account_id: str, mailbox_id: str, name: str, last_sync_at: str | None = None
) -> None:
    """Upsert a mailbox discovered by IMAP sync."""

    db.execute(
        """
        INSERT INTO mailboxes (id, account_id, name, last_sync_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(account_id, name) DO UPDATE SET
          id = excluded.id,
          last_sync_at = COALESCE(excluded.last_sync_at, mailboxes.last_sync_at)
        """,
        (mailbox_id, account_id, name, last_sync_at),
    )


def update_mailbox_state(
    db: Database,
    *,
    mailbox_id: str,
    uid_validity: str | None,
    uid_next: int | None,
    highest_modseq: str | None,
    last_sync_at: str | None = None,
) -> None:
    """Update IMAP mailbox sync metadata."""

    db.execute(
        """
        UPDATE mailboxes
        SET uid_validity = ?, uid_next = ?, highest_modseq = ?, last_sync_at = ?
        WHERE id = ?
        """,
        (uid_validity, uid_next, highest_modseq, last_sync_at or utc_now(), mailbox_id),
    )


def upsert_mail_message(
    db: Database,
    *,
    account_id: str,
    mailbox_id: str,
    message_id: str,
    uid: int,
    subject: str,
    from_address: dict[str, str],
    to_addresses: list[dict[str, str]],
    date: str,
    preview: str,
    body_text: str,
    cc_addresses: list[dict[str, str]] | None = None,
    flags: list[str] | None = None,
    size_bytes: int | None = None,
    has_attachments: bool = False,
) -> None:
    """Upsert a synced mail message and index body text."""

    now = utc_now()
    db.execute(
        """
        INSERT INTO mail_messages
          (id, account_id, mailbox_id, uid, message_id, subject, from_json, to_json, cc_json, date, flags_json,
           size_bytes, preview, body_text, body_hash, has_attachments, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(mailbox_id, uid) DO UPDATE SET
          message_id = excluded.message_id,
          subject = excluded.subject,
          from_json = excluded.from_json,
          to_json = excluded.to_json,
          cc_json = excluded.cc_json,
          date = excluded.date,
          flags_json = excluded.flags_json,
          size_bytes = excluded.size_bytes,
          preview = excluded.preview,
          body_text = excluded.body_text,
          body_hash = excluded.body_hash,
          has_attachments = excluded.has_attachments,
          updated_at = excluded.updated_at,
          deleted_at = NULL
        """,
        (
            message_id,
            account_id,
            mailbox_id,
            uid,
            message_id,
            subject,
            compact_json(from_address),
            compact_json(to_addresses),
            compact_json(cc_addresses or []),
            date,
            compact_json(flags or []),
            size_bytes,
            preview,
            body_text,
            sha256_text(body_text),
            1 if has_attachments else 0,
            now,
        ),
    )
    sender = " ".join([from_address.get("name", ""), from_address.get("email", "")]).strip()
    recipients = " ".join(
        " ".join([address.get("name", ""), address.get("email", "")]).strip() for address in to_addresses
    )
    upsert_search_document(
        db,
        document_id=f"doc_{message_id}",
        domain="mail",
        object_id=message_id,
        title=subject,
        text="\n".join([f"Subject: {subject}", f"From: {sender}", f"Date: {date}", body_text]),
        metadata={
            "date": date,
            "from": from_address,
            "has_attachments": has_attachments,
        },
        sender=sender,
        participants=recipients,
    )


def list_contacts(
    db: Database,
    addressbook_id: str | None,
    limit: int,
    offset: int,
    cursor_secret: str,
) -> dict[str, Any]:
    """List compact contacts."""

    filters = ["deleted_at IS NULL"]
    parameters: list[Any] = []
    if addressbook_id:
        filters.append("addressbook_id = ?")
        parameters.append(addressbook_id)
    rows = db.query(
        f"""
        SELECT id, display_name, emails_json, phones_json, organization
        FROM contacts
        WHERE {" AND ".join(filters)}
        ORDER BY display_name
        LIMIT ? OFFSET ?
        """,
        (*parameters, limit, offset),
    )
    contacts = [_contact_summary(row) for row in rows]
    return {"contacts": contacts, "next_cursor": next_cursor(offset, len(contacts), limit, cursor_secret)}


def upsert_addressbook(
    db: Database,
    *,
    account_id: str,
    addressbook_id: str,
    url: str,
    display_name: str,
    sync_token: str | None = None,
    ctag: str | None = None,
    last_sync_at: str | None = None,
) -> None:
    """Upsert a CardDAV addressbook collection."""

    db.execute(
        """
        INSERT INTO addressbooks (id, account_id, url, display_name, sync_token, ctag, last_sync_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
          id = excluded.id,
          display_name = excluded.display_name,
          sync_token = COALESCE(excluded.sync_token, addressbooks.sync_token),
          ctag = COALESCE(excluded.ctag, addressbooks.ctag),
          last_sync_at = COALESCE(excluded.last_sync_at, addressbooks.last_sync_at)
        """,
        (addressbook_id, account_id, url, display_name, sync_token, ctag, last_sync_at),
    )


def view_contact(db: Database, contact_id: str, include_notes: bool) -> dict[str, Any] | None:
    """Return one contact."""

    row = db.query_one("SELECT * FROM contacts WHERE id = ? AND deleted_at IS NULL", (contact_id,))
    if not row:
        return None
    contact = _contact_summary(row)
    contact["given_name"] = row["given_name"]
    contact["family_name"] = row["family_name"]
    if include_notes:
        contact["notes"] = row["notes"]
    return contact


def upsert_contact(
    db: Database,
    *,
    addressbook_id: str,
    contact_id: str,
    href: str,
    raw_vcard: str,
    display_name: str,
    emails: list[str],
    phones: list[str] | None = None,
    given_name: str | None = None,
    family_name: str | None = None,
    organization: str | None = None,
    notes: str | None = None,
) -> None:
    """Upsert a synced contact, aliases, trigram row, and search document."""

    now = utc_now()
    db.execute(
        """
        INSERT INTO contacts
          (id, addressbook_id, href, raw_vcard, display_name, given_name, family_name,
           emails_json, phones_json, organization, notes, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(addressbook_id, href) DO UPDATE SET
          raw_vcard = excluded.raw_vcard,
          display_name = excluded.display_name,
          given_name = excluded.given_name,
          family_name = excluded.family_name,
          emails_json = excluded.emails_json,
          phones_json = excluded.phones_json,
          organization = excluded.organization,
          notes = excluded.notes,
          updated_at = excluded.updated_at,
          deleted_at = NULL
        """,
        (
            contact_id,
            addressbook_id,
            href,
            raw_vcard,
            display_name,
            given_name,
            family_name,
            compact_json(emails),
            compact_json(phones or []),
            organization,
            notes,
            now,
        ),
    )
    db.execute("DELETE FROM person_aliases WHERE contact_id = ?", (contact_id,))
    aliases = _contact_aliases(display_name, emails, given_name, family_name, organization)
    db.executemany(
        """
        INSERT OR REPLACE INTO person_aliases (alias, normalized_alias, contact_id, alias_type, confidence)
        VALUES (?, ?, ?, ?, ?)
        """,
        [(alias, normalize_text(alias), contact_id, "contact", 0.95) for alias in aliases],
    )
    db.execute("DELETE FROM contact_trigram_fts WHERE contact_id = ?", (contact_id,))
    db.execute(
        """
        INSERT INTO contact_trigram_fts (contact_id, display_name, emails)
        VALUES (?, ?, ?)
        """,
        (contact_id, display_name, " ".join(emails)),
    )
    upsert_search_document(
        db,
        document_id=f"doc_{contact_id}",
        domain="contact",
        object_id=contact_id,
        title=display_name,
        text="\n".join(
            part
            for part in [
                f"Name: {display_name}",
                f"Emails: {', '.join(emails)}",
                f"Phones: {', '.join(phones or [])}",
                f"Organization: {organization or ''}",
            ]
            if part
        ),
        metadata={"emails": emails, "phones": phones or [], "organization": organization},
        participants=" ".join(aliases),
    )


def search_contacts(db: Database, query: str, limit: int) -> dict[str, Any]:
    """Search contacts through alias and trigram tables."""

    normalized = normalize_text(query)
    rows = db.query(
        """
        SELECT DISTINCT c.id, c.display_name, c.emails_json, c.phones_json, c.organization, pa.confidence
        FROM contacts c
        LEFT JOIN person_aliases pa ON pa.contact_id = c.id
        WHERE c.deleted_at IS NULL
          AND (
            pa.normalized_alias LIKE ?
            OR c.display_name LIKE ?
            OR c.emails_json LIKE ?
          )
        ORDER BY COALESCE(pa.confidence, 0.5) DESC, c.display_name
        LIMIT ?
        """,
        (f"%{normalized}%", f"%{query}%", f"%{query}%", limit),
    )
    contacts = []
    for row in rows:
        contact = _contact_summary(row)
        contact["score"] = round(float(row.get("confidence") or 0.5), 3)
        contacts.append(contact)
    return {"contacts": contacts}


def sync_status(db: Database) -> dict[str, Any]:
    """Return sync checkpoint state."""

    checkpoints = db.query("SELECT name, status, last_sync_at, detail_json FROM sync_checkpoints ORDER BY name")
    return {
        "index_generation": index_generation(db),
        "index_freshness": freshness(db),
        "workers": {
            row["name"]: {
                "status": row["status"],
                "last_sync_at": row["last_sync_at"],
                "detail": parse_json(row["detail_json"], {}),
            }
            for row in checkpoints
        },
    }


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
    if start and end:
        try:
            start_dt = datetime.fromisoformat(str(start))
            end_dt = datetime.fromisoformat(str(end))
            if end_dt <= start_dt:
                errors.append("end must be after start")
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


def validate_event_patch(patch: dict[str, Any]) -> list[str]:
    """Validate calendar update patch."""

    if not patch:
        return ["patch must not be empty"]
    input_data = {
        "title": patch.get("title", "existing event"),
        "start": patch.get("start", "2026-01-01T00:00:00+00:00"),
        "end": patch.get("end", "2026-01-01T01:00:00+00:00"),
        "timezone": patch.get("timezone", "UTC"),
        "attendees": patch.get("attendees", []),
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


def _ics_temporal_value(value: str, timezone: str) -> datetime | date:
    if "T" not in value:
        return date.fromisoformat(value)

    parsed = datetime.fromisoformat(value)
    named_timezone = ZoneInfo(timezone)
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


def _contact_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "display_name": row["display_name"],
        "emails": parse_json(row.get("emails_json"), []),
        "phones": parse_json(row.get("phones_json"), []),
        "organization": row.get("organization"),
    }


def _contact_aliases(
    display_name: str,
    emails: list[str],
    given_name: str | None,
    family_name: str | None,
    organization: str | None,
) -> list[str]:
    aliases = [display_name, *emails]
    if given_name:
        aliases.append(given_name)
    if family_name:
        aliases.append(family_name)
    if organization:
        aliases.append(organization)
    return [alias for alias in dict.fromkeys(alias.strip() for alias in aliases) if alias]
