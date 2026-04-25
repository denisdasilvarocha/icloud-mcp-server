"""Search index repository interface."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from icloud_mcp.platform.util import compact_json, normalize_text, parse_json, sha256_text, tokenize, truncate, utc_now
from icloud_mcp.search.chunker import chunk_text
from icloud_mcp.search.query_cache import query_cache_get as query_cache_get
from icloud_mcp.search.query_cache import query_cache_set as query_cache_set
from icloud_mcp.search.rerank import reciprocal_rank_score
from icloud_mcp.search.vector import VECTOR_MODEL, cosine_score, cosine_score_vectors, embedding_vector
from icloud_mcp.search.vector_backend import delete_document_vectors, query_similar_chunks
from icloud_mcp.storage.cache_state import bump_index_generation
from icloud_mcp.storage.connection import Database

MIN_SEMANTIC_SCORE = 0.2
MIN_SQLITE_VEC_SCORE = 0.1


@dataclass(frozen=True)
class SearchIndexQuery:
    """Resolved query inputs for local search index reads."""

    query: str
    domains: list[str]
    limit: int
    offset: int
    snippet_chars: int
    start: str | None = None
    end: str | None = None
    person: str | None = None


def search_index(db: Database, query: SearchIndexQuery) -> list[dict[str, Any]]:
    """Read compact search rows from the local search index."""

    return search_documents(
        db,
        query=query.query,
        domains=query.domains,
        limit=query.limit,
        offset=query.offset,
        snippet_chars=query.snippet_chars,
        start=query.start,
        end=query.end,
        person=query.person,
    )


def _datetime_value(value: str | None, timezone: str) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
    return parsed


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
    query_limit = min(max(limit * 5, limit + 1), 100)
    if terms:
        fts_query = " OR ".join(f'"{term}"' for term in terms[:8])
        placeholders = ",".join("?" for _ in domains)
        rows = db.query(
            f"""
            WITH raw_matches AS (
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
            ),
            ranked_matches AS (
              SELECT
                *,
                ROW_NUMBER() OVER (
                  PARTITION BY domain, object_id
                  ORDER BY rank ASC, id ASC
                ) AS object_rank
              FROM raw_matches
            )
            SELECT
              id, domain, object_id, occurrence_id, title, canonical_text, metadata_json, matched_text, rank
            FROM ranked_matches
            WHERE object_rank = 1
            ORDER BY rank ASC, id ASC
            LIMIT ? OFFSET ?
            """,
            (fts_query, *domains, query_limit, offset),
        )
    else:
        placeholders = ",".join("?" for _ in domains)
        rows = db.query(
            f"""
            WITH ranked_documents AS (
              SELECT
                id, domain, object_id, occurrence_id, title, canonical_text, metadata_json, updated_at, 0.0 AS rank,
                ROW_NUMBER() OVER (
                  PARTITION BY domain, object_id
                  ORDER BY updated_at DESC, id ASC
                ) AS object_rank
              FROM search_documents
              WHERE deleted_at IS NULL AND domain IN ({placeholders})
            )
            SELECT id, domain, object_id, occurrence_id, title, canonical_text, metadata_json, rank
            FROM ranked_documents
            WHERE object_rank = 1
            ORDER BY updated_at DESC, id ASC
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
    if len(rows) >= limit:
        return rows
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
        if score < MIN_SEMANTIC_SCORE:
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
        score = 1.0 - distance if distance <= 1.0 else 1.0 / (1.0 + distance)
        if score < MIN_SQLITE_VEC_SCORE or cosine_score(query, row.get("matched_text") or row["canonical_text"]) <= 0:
            continue
        row["score"] = score
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
        or (range_end_dt is not None and item_start_dt >= range_end_dt)
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
