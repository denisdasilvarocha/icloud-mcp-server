"""Repository functions for local cache and search index."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr
from icalendar import Alarm, Calendar, Event

from icloud_mcp.config import Settings
from icloud_mcp.db.connection import Database
from icloud_mcp.indexing.chunker import chunk_text
from icloud_mcp.indexing.rerank import reciprocal_rank_score
from icloud_mcp.indexing.vector import VECTOR_MODEL, cosine_score, cosine_score_vectors, embedding_vector
from icloud_mcp.indexing.vector_backend import delete_document_vectors, query_similar_chunks
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
    backfill = db.query_one(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN backfill_status = 'complete' THEN 1 ELSE 0 END) AS complete
        FROM mailboxes
        """
    )
    return {
        "mail_last_sync": mail["value"] if mail else None,
        "calendar_last_sync": calendar["value"] if calendar else None,
        "contacts_last_sync": contacts["value"] if contacts else None,
        "mail_backfill_status": _mail_backfill_status(backfill),
    }


def freshness_status(db: Database, stale_after_seconds: int) -> dict[str, dict[str, Any]]:
    """Return freshness timestamps plus healthy/stale classification."""

    values = freshness(db)
    now = datetime.now(tz=UTC)
    statuses: dict[str, dict[str, Any]] = {}
    for domain, key in [
        ("mail", "mail_last_sync"),
        ("calendar", "calendar_last_sync"),
        ("contacts", "contacts_last_sync"),
    ]:
        synced_at = values.get(key)
        age_seconds = None
        status = "never_synced"
        reason = "no successful sync timestamp"
        if synced_at:
            age_seconds = max(0, int((now - datetime.fromisoformat(str(synced_at))).total_seconds()))
            status = "stale" if age_seconds > stale_after_seconds else "healthy"
            reason = f"older than {stale_after_seconds}s" if status == "stale" else "within freshness threshold"
        statuses[domain] = {
            "last_sync_at": synced_at,
            "age_seconds": age_seconds,
            "status": status,
            "reason": reason,
        }
    statuses["mail"]["backfill_status"] = values.get("mail_backfill_status")
    return statuses


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
    chunks: list[dict[str, Any]] | None = None,
) -> None:
    """Upsert one search document, chunks, and FTS rows."""

    now = utc_now()
    metadata_json = compact_json(metadata or {})
    chunk_rows = (
        chunks
        or [{"text": part, "type": "body"} for part in chunk_text(text, 4000)]
        or [{"text": text, "type": "body"}]
    )

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
    delete_document_vectors(db, document_id)
    db.execute(
        "DELETE FROM search_embeddings WHERE chunk_id IN (SELECT id FROM search_chunks WHERE document_id = ?)",
        (document_id,),
    )
    db.execute("DELETE FROM search_chunks WHERE document_id = ?", (document_id,))
    db.execute("DELETE FROM search_fts WHERE document_id = ?", (document_id,))
    for chunk_index, chunk in enumerate(chunk_rows):
        chunk_text_value = str(chunk.get("text") or "")
        chunk_metadata = {
            **(metadata or {}),
            **dict(chunk.get("metadata") or {}),
            "chunk_type": chunk.get("type", "body"),
        }
        chunk_id = f"{document_id}:{chunk_index}"
        db.execute(
            """
            INSERT INTO search_chunks
              (id, document_id, chunk_index, chunk_type, text, token_count, text_hash, metadata_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chunk_id,
                document_id,
                chunk_index,
                str(chunk.get("type") or "body"),
                chunk_text_value,
                len(tokenize(chunk_text_value)),
                sha256_text(chunk_text_value),
                compact_json(chunk_metadata),
                now,
            ),
        )
        db.execute(
            """
            INSERT INTO search_fts (document_id, object_id, domain, title, text, sender, participants)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (document_id, object_id, domain, title, chunk_text_value, sender, participants),
        )
    db.execute(
        """
        INSERT OR REPLACE INTO search_embeddings (chunk_id, embedding_model, vector_json, updated_at)
        SELECT id, ?, ?, ?
        FROM search_chunks
        WHERE document_id = ? AND chunk_index = 0
        """,
        (VECTOR_MODEL, compact_json(embedding_vector(text)), now, document_id),
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
    start: str | None = None,
    end: str | None = None,
    person: str | None = None,
) -> list[dict[str, Any]]:
    """Run local lexical FTS search and return compact rows."""

    terms = tokenize(query)
    query_limit = min(limit * 5, 100) if (start or end or person) else limit
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
              search_fts.text AS matched_text,
              bm25(search_fts) AS rank
            FROM search_fts
            JOIN search_documents d ON d.id = search_fts.document_id
            WHERE search_fts MATCH ?
              AND d.deleted_at IS NULL
              AND d.domain IN ({placeholders})
            ORDER BY rank ASC
            LIMIT ? OFFSET ?
            """,
            (fts_query, *domains, query_limit, offset),
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
            (*domains, query_limit, offset),
        )

    rows = _rerank_rows(_add_semantic_results(db, query=query, domains=domains, rows=rows, limit=query_limit))

    results = []
    seen_documents: set[str] = set()
    seen_objects: set[tuple[str, str]] = set()
    for index, row in enumerate(rows):
        if row["id"] in seen_documents:
            continue
        seen_documents.add(row["id"])
        object_key = (row["domain"], row["object_id"])
        if object_key in seen_objects:
            continue
        seen_objects.add(object_key)
        metadata = parse_json(row.get("metadata_json"), {})
        if not _matches_search_filters(row, metadata, start=start, end=end, person=person):
            continue
        score = row.get("score")
        if score is None:
            score = _weighted_score(row, metadata, offset + index)
        snippet_text = row.get("matched_text") or row.get("canonical_text")
        results.append(
            {
                "id": row["object_id"],
                "document_id": row["id"],
                "occurrence_id": row.get("occurrence_id"),
                "domain": "contacts" if row["domain"] == "contact" else row["domain"],
                "title": truncate(row.get("title"), 120),
                "snippet": truncate(snippet_text, snippet_chars),
                "score": round(float(score), 3),
                "why": row.get("why", ["lexical_match"] if terms else ["recent_indexed_item"]),
                "content_trust": "untrusted_user_data",
                **metadata,
            }
        )
        if len(results) >= limit:
            break
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
    semantic_rows = _sqlite_vec_semantic_results(db, query=query, domains=domains, existing=existing, limit=limit)
    if semantic_rows:
        return rows + semantic_rows[: max(0, limit - len(rows))]
    placeholders = ",".join("?" for _ in domains)
    candidates = db.query(
        f"""
        SELECT d.id, d.domain, d.object_id, d.occurrence_id, d.title, d.canonical_text, d.metadata_json,
               e.vector_json
        FROM search_documents d
        LEFT JOIN search_chunks c ON c.document_id = d.id AND c.chunk_index = 0
        LEFT JOIN search_embeddings e ON e.chunk_id = c.id
        WHERE d.deleted_at IS NULL AND d.domain IN ({placeholders})
        """,
        (*domains,),
    )
    semantic_rows = []
    for candidate in candidates:
        if candidate["id"] in existing:
            continue
        query_vector = embedding_vector(query)
        vector = parse_json(candidate.get("vector_json"), None)
        score = (
            cosine_score_vectors(query_vector, vector)
            if isinstance(vector, dict)
            else cosine_score(query, candidate["canonical_text"])
        )
        if score <= 0:
            continue
        candidate["score"] = score
        candidate["why"] = ["semantic_match"]
        semantic_rows.append(candidate)
    semantic_rows.sort(key=lambda row: row["score"], reverse=True)
    return rows + semantic_rows[: max(0, limit - len(rows))]


def _rerank_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    scores: dict[str, float] = {}
    merged: dict[str, dict[str, Any]] = {}
    whys: dict[str, list[str]] = {}
    for rank, row in enumerate(rows, start=1):
        document_id = row["id"]
        merged.setdefault(document_id, dict(row))
        why_values = row.get("why") or ["lexical_match"]
        whys.setdefault(document_id, [])
        for why in why_values:
            if why not in whys[document_id]:
                whys[document_id].append(why)
        weight = 1.2 if {"semantic_match", "sqlite_vec_match"} & set(why_values) else 1.0
        scores[document_id] = scores.get(document_id, 0.0) + reciprocal_rank_score(rank) * weight
        if row.get("score") is not None:
            scores[document_id] += max(0.0, float(row["score"])) * 0.2
    max_score = max(scores.values()) or 1.0
    reranked = []
    for document_id, row in merged.items():
        row["score"] = round(scores[document_id] / max_score, 3)
        row["why"] = whys.get(document_id) or ["lexical_match"]
        reranked.append(row)
    reranked.sort(key=lambda row: row["score"], reverse=True)
    return reranked


def _sqlite_vec_semantic_results(
    db: Database,
    *,
    query: str,
    domains: list[str],
    existing: set[str],
    limit: int,
) -> list[dict[str, Any]]:
    nearest = query_similar_chunks(db, query, max(limit * 5, 10))
    if not nearest:
        return []
    placeholders = ",".join("?" for _ in nearest)
    domain_placeholders = ",".join("?" for _ in domains)
    distances = {row["chunk_id"]: float(row["distance"]) for row in nearest}
    rows = db.query(
        f"""
        SELECT d.id, d.domain, d.object_id, d.occurrence_id, d.title, d.canonical_text, d.metadata_json,
               c.text AS matched_text, c.id AS chunk_id
        FROM search_chunks c
        JOIN search_documents d ON d.id = c.document_id
        WHERE c.id IN ({placeholders})
          AND d.deleted_at IS NULL
          AND d.domain IN ({domain_placeholders})
        """,
        (*distances.keys(), *domains),
    )
    semantic_rows = []
    for row in rows:
        if row["id"] in existing:
            continue
        distance = distances.get(row["chunk_id"], 1.0)
        row["score"] = max(0.0, 1.0 - distance)
        row["why"] = ["sqlite_vec_match"]
        semantic_rows.append(row)
    semantic_rows.sort(key=lambda row: row["score"], reverse=True)
    return semantic_rows


def person_alias_terms(db: Database, person: str | None) -> list[str]:
    """Return known aliases for a person query."""

    if not person:
        return []
    normalized = normalize_text(person)
    rows = db.query(
        """
        SELECT DISTINCT alias
        FROM person_aliases
        WHERE normalized_alias LIKE ?
        ORDER BY confidence DESC, alias
        LIMIT 8
        """,
        (f"%{normalized}%",),
    )
    aliases = [row["alias"] for row in rows]
    return list(dict.fromkeys([person, *aliases]))


def query_cache_get(db: Database, key: str, generation: int) -> dict[str, Any] | None:
    """Return a valid cached search response."""

    now = utc_now()
    row = db.query_one(
        """
        SELECT value_json
        FROM query_cache
        WHERE key = ? AND index_generation = ? AND expires_at > ?
        """,
        (key, generation, now),
    )
    return parse_json(row["value_json"], {}) if row else None


def query_cache_set(db: Database, key: str, value: dict[str, Any], generation: int, ttl_seconds: int = 300) -> None:
    """Store a short-lived search response cache entry."""

    expires_at = (datetime.now(tz=UTC) + timedelta(seconds=ttl_seconds)).replace(microsecond=0).isoformat()
    db.execute(
        """
        INSERT INTO query_cache (key, value_json, expires_at, index_generation)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
          value_json = excluded.value_json,
          expires_at = excluded.expires_at,
          index_generation = excluded.index_generation
        """,
        (key, compact_json(value), expires_at, generation),
    )


def _matches_search_filters(
    row: dict[str, Any],
    metadata: dict[str, Any],
    *,
    start: str | None,
    end: str | None,
    person: str | None,
) -> bool:
    if (start or end) and not _matches_time_filter(row, metadata, start=start, end=end):
        return False
    return not (person and not _matches_person_filter(row, metadata, person))


def _weighted_score(row: dict[str, Any], metadata: dict[str, Any], rank_index: int) -> float:
    lexical = max(0.0, 1.0 - rank_index * 0.04)
    domain_boost = 0.08 if row.get("domain") == "calendar" and metadata.get("time") else 0.0
    freshness_boost = 0.05 if _is_upcoming(metadata) else 0.0
    source_quality = -0.25 if metadata.get("source_quality") in {"spam", "junk", "newsletter"} else 0.0
    return max(0.0, min(1.0, lexical + domain_boost + freshness_boost + source_quality))


def _is_upcoming(metadata: dict[str, Any]) -> bool:
    time_value = metadata.get("time") if isinstance(metadata.get("time"), dict) else {}
    item_start = time_value.get("start") or metadata.get("date")
    if not item_start:
        return False
    try:
        parsed = datetime.fromisoformat(str(item_start))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed >= datetime.now(tz=UTC)
    except (TypeError, ValueError):
        return False


def _mail_backfill_status(row: dict[str, Any] | None) -> str:
    if not row or not row.get("total"):
        return "not_started"
    complete = int(row.get("complete") or 0)
    total = int(row.get("total") or 0)
    if complete == total:
        return "complete"
    if complete > 0:
        return "partial"
    return "not_started"


def _matches_time_filter(row: dict[str, Any], metadata: dict[str, Any], *, start: str | None, end: str | None) -> bool:
    time_value = metadata.get("time") if isinstance(metadata.get("time"), dict) else {}
    timezone = time_value.get("timezone") or "UTC"
    item_start = time_value.get("start") or metadata.get("date")
    item_end = time_value.get("end") or item_start
    if not item_start:
        return row.get("domain") == "contact"
    item_start_dt = _datetime_value(str(item_start), timezone)
    item_end_dt = _datetime_value(str(item_end), timezone)
    range_start_dt = _datetime_value(start, timezone) if start else None
    range_end_dt = _datetime_value(end, timezone) if end else None
    return not (
        (range_start_dt is not None and item_end_dt < range_start_dt)
        or (range_end_dt is not None and item_start_dt > range_end_dt)
    )


def _matches_person_filter(row: dict[str, Any], metadata: dict[str, Any], person: str) -> bool:
    needle = normalize_text(person)
    haystack_parts = [
        row.get("title", ""),
        row.get("canonical_text", ""),
        " ".join(metadata.get("participants", [])) if isinstance(metadata.get("participants"), list) else "",
        compact_json(metadata.get("from", {})) if isinstance(metadata.get("from"), dict) else "",
        " ".join(metadata.get("emails", [])) if isinstance(metadata.get("emails"), list) else "",
    ]
    return needle in normalize_text(" ".join(haystack_parts))


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


def tombstone_contact(db: Database, contact_id: str) -> None:
    """Mark a contact deleted and cleanup aliases/search rows."""

    now = utc_now()
    db.execute("UPDATE contacts SET deleted_at = ? WHERE id = ?", (now, contact_id))
    db.execute("DELETE FROM person_aliases WHERE contact_id = ?", (contact_id,))
    db.execute("DELETE FROM contact_trigram_fts WHERE contact_id = ?", (contact_id,))
    db.execute(
        "UPDATE search_documents SET deleted_at = ? WHERE object_id = ? AND domain = 'contact'", (now, contact_id)
    )
    db.execute("DELETE FROM search_fts WHERE object_id = ? AND domain = 'contact'", (contact_id,))
    bump_index_generation(db)


def tombstone_mail_message(db: Database, message_id: str) -> None:
    """Mark a mail message deleted and cleanup search rows."""

    now = utc_now()
    db.execute("UPDATE mail_messages SET deleted_at = ? WHERE id = ?", (now, message_id))
    db.execute(
        "UPDATE search_documents SET deleted_at = ? WHERE object_id = ? AND domain IN ('mail','mail_invite')",
        (now, message_id),
    )
    db.execute("DELETE FROM search_fts WHERE object_id = ? AND domain IN ('mail','mail_invite')", (message_id,))
    bump_index_generation(db)


def tombstone_mail_message_by_uid(db: Database, mailbox_id: str, uid: int) -> None:
    """Mark a mail message deleted by IMAP mailbox UID."""

    row = db.query_one(
        "SELECT id FROM mail_messages WHERE mailbox_id = ? AND uid = ? AND deleted_at IS NULL",
        (mailbox_id, uid),
    )
    if row:
        tombstone_mail_message(db, row["id"])


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

    event_id = f"cal_evt_{uuid.uuid5(uuid.NAMESPACE_URL, request_id).hex}" if request_id else f"cal_evt_{uuid.uuid4().hex}"
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
        key: value
        for key, value in original_recurrence.items()
        if key.casefold() not in {"count", "until"}
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
            f"cal_occ_{uuid.uuid4().hex}",
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


def view_mail(
    db: Database,
    message_id: str,
    include: list[str],
    max_body_chars: int,
    body_offset: int = 0,
) -> dict[str, Any] | None:
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
            "bcc": parse_json(row.get("bcc_json"), []),
            "message_id": row["message_id"],
            "in_reply_to": row.get("in_reply_to"),
            "references": parse_json(row.get("references_json"), []),
            "flags": parse_json(row["flags_json"], []),
        }
    if "body_text" in include:
        body = row["body_text"] or ""
        safe_offset = max(0, min(body_offset, len(body)))
        body_end = safe_offset + max_body_chars
        result["body_text"] = body[safe_offset:body_end]
        result["body_truncated"] = body_end < len(body)
        result["body_unavailable_reason"] = row.get("body_unavailable_reason")
        next_offset = body_end if body_end < len(body) else None
        result["body_continuation"] = {
            "available": next_offset is not None,
            "offset": safe_offset,
            "next_offset": next_offset,
            "returned_chars": len(result["body_text"]),
            "total_chars": len(body),
            "indexed_chars": row.get("body_indexed_chars") or 0,
        }
    if "attachments" in include:
        result["attachments"] = parse_json(row.get("attachments_json"), [])
    result["content_trust"] = "untrusted_user_data"
    return result


def mailboxes_for_backfill(db: Database, limit: int) -> list[dict[str, Any]]:
    """Return mailboxes with older mail backfill still pending."""

    return db.query(
        """
        SELECT id, name, backfill_cursor, backfill_status
        FROM mailboxes
        WHERE COALESCE(backfill_status, 'not_started') != 'complete'
        ORDER BY last_sync_at DESC, name ASC
        LIMIT ?
        """,
        (limit,),
    )


def upsert_mailbox(
    db: Database, *, account_id: str, mailbox_id: str, name: str, last_sync_at: str | None = None
) -> None:
    """Upsert a mailbox discovered by IMAP sync."""

    db.execute(
        """
        INSERT INTO mailboxes (id, account_id, name, folder_quality, backfill_status, last_sync_at)
        VALUES (?, ?, ?, ?, 'not_started', ?)
        ON CONFLICT(account_id, name) DO UPDATE SET
          id = excluded.id,
          folder_quality = excluded.folder_quality,
          last_sync_at = COALESCE(excluded.last_sync_at, mailboxes.last_sync_at)
        """,
        (mailbox_id, account_id, name, _mailbox_quality(name), last_sync_at),
    )


def update_mailbox_state(
    db: Database,
    *,
    mailbox_id: str,
    uid_validity: str | None,
    uid_next: int | None,
    highest_modseq: str | None,
    last_synced_uid: int | None = None,
    backfill_cursor: str | None = None,
    backfill_status: str | None = None,
    last_sync_at: str | None = None,
) -> None:
    """Update IMAP mailbox sync metadata."""

    db.execute(
        """
        UPDATE mailboxes
        SET uid_validity = ?,
            uid_next = ?,
            highest_modseq = ?,
            last_synced_uid = COALESCE(?, last_synced_uid),
            backfill_cursor = COALESCE(?, backfill_cursor),
            backfill_status = COALESCE(?, backfill_status),
            last_sync_at = ?
        WHERE id = ?
        """,
        (
            uid_validity,
            uid_next,
            highest_modseq,
            last_synced_uid,
            backfill_cursor,
            backfill_status,
            last_sync_at or utc_now(),
            mailbox_id,
        ),
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
    bcc_addresses: list[dict[str, str]] | None = None,
    header_message_id: str | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    flags: list[str] | None = None,
    size_bytes: int | None = None,
    has_attachments: bool = False,
    attachments: list[dict[str, Any]] | None = None,
    calendar_invites: list[dict[str, Any]] | None = None,
    body_unavailable_reason: str | None = None,
    max_index_chars: int = 16000,
) -> None:
    """Upsert a synced mail message and index body text."""

    now = utc_now()
    indexed_body = _searchable_mail_body(body_text)[:max_index_chars]
    thread_id = _mail_thread_id(header_message_id or message_id, in_reply_to, references or [])
    db.execute(
        """
        INSERT INTO mail_messages
          (id, account_id, mailbox_id, uid, message_id, thread_id, subject, from_json, to_json, cc_json, bcc_json,
           in_reply_to, references_json, date, flags_json, size_bytes, preview, body_text, body_hash,
           body_unavailable_reason, body_indexed_chars, has_attachments, attachments_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(mailbox_id, uid) DO UPDATE SET
          message_id = excluded.message_id,
          thread_id = excluded.thread_id,
          subject = excluded.subject,
          from_json = excluded.from_json,
          to_json = excluded.to_json,
          cc_json = excluded.cc_json,
          bcc_json = excluded.bcc_json,
          in_reply_to = excluded.in_reply_to,
          references_json = excluded.references_json,
          date = excluded.date,
          flags_json = excluded.flags_json,
          size_bytes = excluded.size_bytes,
          preview = excluded.preview,
          body_text = excluded.body_text,
          body_hash = excluded.body_hash,
          body_unavailable_reason = excluded.body_unavailable_reason,
          body_indexed_chars = excluded.body_indexed_chars,
          has_attachments = excluded.has_attachments,
          attachments_json = excluded.attachments_json,
          updated_at = excluded.updated_at,
          deleted_at = NULL
        """,
        (
            message_id,
            account_id,
            mailbox_id,
            uid,
            header_message_id or message_id,
            thread_id,
            subject,
            compact_json(from_address),
            compact_json(to_addresses),
            compact_json(cc_addresses or []),
            compact_json(bcc_addresses or []),
            in_reply_to,
            compact_json(references or []),
            date,
            compact_json(flags or []),
            size_bytes,
            preview,
            body_text,
            sha256_text(body_text),
            body_unavailable_reason,
            len(indexed_body),
            1 if has_attachments else 0,
            compact_json(attachments or []),
            now,
        ),
    )
    sender = " ".join([from_address.get("name", ""), from_address.get("email", "")]).strip()
    recipients = " ".join(
        " ".join([address.get("name", ""), address.get("email", "")]).strip()
        for address in [*to_addresses, *(cc_addresses or []), *(bcc_addresses or [])]
    )
    mailbox = db.query_one("SELECT name, folder_quality FROM mailboxes WHERE id = ?", (mailbox_id,)) or {}
    metadata = {
        "date": date,
        "from": from_address,
        "to": to_addresses,
        "mailbox": mailbox.get("name"),
        "source_quality": mailbox.get("folder_quality") or "normal",
        "has_attachments": has_attachments,
        "attachments": attachments or [],
        "body_unavailable_reason": body_unavailable_reason,
    }
    upsert_search_document(
        db,
        document_id=f"doc_{message_id}",
        domain="mail",
        object_id=message_id,
        title=subject,
        text="\n".join([f"Subject: {subject}", f"From: {sender}", f"Date: {date}", indexed_body]),
        metadata=metadata,
        sender=sender,
        participants=recipients,
        chunks=_mail_chunks(
            subject, from_address, to_addresses, cc_addresses or [], bcc_addresses or [], preview, indexed_body
        ),
    )
    for invite in calendar_invites or []:
        _index_mail_invite(db, message_id=message_id, subject=subject, sender=sender, invite=invite)


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
    contact["content_trust"] = "untrusted_user_data"
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
    extra_aliases: list[tuple[str, str, float]] | None = None,
) -> None:
    """Upsert a synced contact, aliases, trigram row, and search document."""

    now = utc_now()
    raw_phones = phones or []
    normalized_phones = [_normalize_phone_alias(phone) for phone in raw_phones]
    phone_aliases = [phone for phone in normalized_phones if phone]
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
            compact_json(raw_phones),
            organization,
            notes,
            now,
        ),
    )
    db.execute("DELETE FROM person_aliases WHERE contact_id = ?", (contact_id,))
    aliases = _contact_aliases(display_name, emails, given_name, family_name, organization)
    typed_aliases = [
        (alias, "email" if "@" in alias else "name", 0.95 if alias == display_name else 0.85) for alias in aliases
    ]
    for email in emails:
        local_part = email.split("@", 1)[0]
        if local_part:
            typed_aliases.append((local_part, "email_local_part", 0.7))
    typed_aliases.extend((phone, "phone_e164", 0.75) for phone in phone_aliases)
    typed_aliases.extend(extra_aliases or [])
    db.executemany(
        """
        INSERT OR REPLACE INTO person_aliases (alias, normalized_alias, contact_id, alias_type, confidence)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (alias, normalize_text(alias), contact_id, alias_type, confidence)
            for alias, alias_type, confidence in typed_aliases
        ],
    )
    db.execute("DELETE FROM contact_trigram_fts WHERE contact_id = ?", (contact_id,))
    db.execute(
        """
        INSERT INTO contact_trigram_fts (contact_id, display_name, emails)
        VALUES (?, ?, ?)
        """,
        (contact_id, display_name, " ".join([*emails, *raw_phones, *phone_aliases])),
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
                f"Phones: {', '.join(raw_phones)}",
                f"Organization: {organization or ''}",
            ]
            if part
        ),
        metadata={"emails": emails, "phones": raw_phones, "phone_aliases": phone_aliases, "organization": organization},
        participants=" ".join(aliases),
    )


def _normalize_phone_alias(phone: str) -> str | None:
    digits = "".join(char for char in phone if char.isdigit())
    if not digits:
        return None
    if phone.strip().startswith("+"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return digits


def search_contacts(
    db: Database,
    query: str,
    limit: int,
    offset: int = 0,
    cursor_secret: str | None = None,
) -> dict[str, Any]:
    """Search contacts through alias and trigram tables."""

    normalized = normalize_text(query)
    trigram_query = '"' + query.replace('"', '""') + '"'
    trigram_rows = db.query(
        """
        SELECT c.id, c.display_name, c.emails_json, c.phones_json, c.organization, 0.72 AS confidence
        FROM contact_trigram_fts f
        JOIN contacts c ON c.id = f.contact_id
        WHERE contact_trigram_fts MATCH ? AND c.deleted_at IS NULL
        LIMIT ? OFFSET ?
        """,
        (trigram_query, limit, offset),
    )
    rows = db.query(
        """
        SELECT c.id, c.display_name, c.emails_json, c.phones_json, c.organization, MAX(pa.confidence) AS confidence
        FROM contacts c
        LEFT JOIN person_aliases pa ON pa.contact_id = c.id
        WHERE c.deleted_at IS NULL
          AND (
            pa.normalized_alias LIKE ?
            OR c.display_name LIKE ?
            OR c.emails_json LIKE ?
          )
        GROUP BY c.id, c.display_name, c.emails_json, c.phones_json, c.organization
        ORDER BY COALESCE(MAX(pa.confidence), 0.5) DESC, c.display_name
        LIMIT ? OFFSET ?
        """,
        (f"%{normalized}%", f"%{query}%", f"%{query}%", limit, offset),
    )
    merged: dict[str, dict[str, Any]] = {}
    for row in [*rows, *trigram_rows]:
        existing = merged.get(row["id"])
        if existing and float(existing.get("confidence") or 0) >= float(row.get("confidence") or 0):
            continue
        merged[row["id"]] = row
    contacts = []
    for row in sorted(merged.values(), key=lambda item: (-(float(item.get("confidence") or 0.5)), item["display_name"])):
        contact = _contact_summary(row)
        contact["score"] = round(float(row.get("confidence") or 0.5), 3)
        contacts.append(contact)
    response = {"contacts": contacts}
    if cursor_secret:
        response["next_cursor"] = next_cursor(offset, len(contacts), limit, cursor_secret)
    return response


def sync_status(db: Database, stale_after_seconds: int = 86400) -> dict[str, Any]:
    """Return sync checkpoint state."""

    checkpoints = db.query(
        """
        SELECT name, status, last_sync_at, last_error, retry_count, backoff_until, progress_cursor, detail_json
        FROM sync_checkpoints
        ORDER BY name
        """
    )
    return {
        "index_generation": index_generation(db),
        "index_freshness": freshness(db),
        "freshness_status": freshness_status(db, stale_after_seconds),
        "workers": {
            row["name"]: {
                "status": row["status"],
                "last_sync_at": row["last_sync_at"],
                "last_error": row.get("last_error"),
                "retry_count": row.get("retry_count") or 0,
                "backoff_until": row.get("backoff_until"),
                "progress_cursor": row.get("progress_cursor"),
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


def _mailbox_quality(name: str) -> str:
    normalized = normalize_text(name)
    if any(part in normalized for part in ["spam", "junk", "trash", "deleted"]):
        return "spam"
    if any(part in normalized for part in ["newsletter", "promotions", "bulk"]):
        return "newsletter"
    return "normal"


def _mail_thread_id(header_message_id: str, in_reply_to: str | None, references: list[str]) -> str:
    source = references[0] if references else in_reply_to or header_message_id
    return f"thread_{sha256_text(source)[:24]}"


def _searchable_mail_body(body_text: str) -> str:
    lines = []
    quote_started = False
    for line in body_text.splitlines():
        stripped = line.strip()
        lowered = stripped.casefold()
        if not stripped:
            continue
        if stripped.startswith(">") or lowered.startswith("on ") and lowered.endswith("wrote:"):
            quote_started = True
            continue
        if lowered in {"original message", "forwarded message"} or lowered.startswith("from: "):
            quote_started = True
            continue
        if not quote_started:
            lines.append(stripped)
    return "\n".join(lines) or body_text


def _mail_chunks(
    subject: str,
    from_address: dict[str, str],
    to_addresses: list[dict[str, str]],
    cc_addresses: list[dict[str, str]],
    bcc_addresses: list[dict[str, str]],
    preview: str,
    body_text: str,
) -> list[dict[str, Any]]:
    header_text = "\n".join(
        [
            f"Subject: {subject}",
            f"From: {from_address.get('name', '')} {from_address.get('email', '')}",
            f"To: {_addresses_text(to_addresses)}",
            f"Cc: {_addresses_text(cc_addresses)}",
            f"Bcc: {_addresses_text(bcc_addresses)}",
            f"Preview: {preview}",
        ]
    )
    chunks = [{"type": "header", "text": header_text}]
    chunks.extend({"type": "body", "text": chunk} for chunk in chunk_text(body_text, 4000))
    return chunks


def _addresses_text(addresses: list[dict[str, str]]) -> str:
    return " ".join(" ".join([address.get("name", ""), address.get("email", "")]).strip() for address in addresses)


def _index_mail_invite(db: Database, *, message_id: str, subject: str, sender: str, invite: dict[str, Any]) -> None:
    title = invite.get("summary") or subject
    text = "\n".join(
        str(part)
        for part in [
            f"Invite: {title}",
            f"Method: {invite.get('method') or ''}",
            f"UID: {invite.get('uid') or ''}",
            f"Start: {invite.get('start') or ''}",
            f"End: {invite.get('end') or ''}",
            f"Organizer: {invite.get('organizer') or ''}",
            f"Attendees: {' '.join(invite.get('attendees') or [])}",
        ]
        if part
    )
    upsert_search_document(
        db,
        document_id=f"doc_{message_id}_invite_{sha256_text(text)[:12]}",
        domain="mail_invite",
        object_id=message_id,
        title=str(title),
        text=text,
        metadata={
            "source_mail_id": message_id,
            "invite": invite,
            "time": {"start": invite.get("start"), "end": invite.get("end"), "timezone": invite.get("timezone")},
        },
        sender=sender,
        participants=" ".join(invite.get("attendees") or []),
        chunks=[{"type": "invite", "text": text}],
    )
