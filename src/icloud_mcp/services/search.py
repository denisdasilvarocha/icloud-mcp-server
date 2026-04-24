"""Search service coordinating query planning, cache policy, and repositories."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from icloud_mcp.config import Settings
from icloud_mcp.db.connection import Database
from icloud_mcp.db.repositories import (
    freshness,
    freshness_status,
    index_generation,
    person_alias_terms,
    query_cache_get,
    query_cache_set,
    search_documents,
)
from icloud_mcp.indexing.query_planner import plan_query
from icloud_mcp.util import compact_json, next_cursor, normalize_text, sha256_text, tokenize


class SearchService:
    """Local-first search policy boundary for MCP tools."""

    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    def search(
        self,
        *,
        query: str,
        domains: list[str] | None,
        start: datetime | None,
        end: datetime | None,
        person: str | None,
        limit: int,
        include_body_snippets: bool,
        freshness_policy: str,
        cursor_payload: dict[str, Any],
    ) -> dict[str, Any]:
        plan = plan_query(query)
        selected_domains = list(domains or _external_domains(plan.domains))
        db_domains = ["contact" if domain == "contacts" else domain for domain in selected_domains]
        safe_limit = max(1, min(limit, 50))
        offset = int(cursor_payload.get("offset", 0))
        planned_people = [person] if person else plan.people or []
        person_terms = []
        for planned_person in planned_people:
            person_terms.extend(person_alias_terms(self.db, planned_person))
        effective_query = " ".join(part for part in [query, " ".join(person_terms)] if part).strip()
        effective_start = start.isoformat() if start else plan.start
        effective_end = end.isoformat() if end else plan.end
        generation = index_generation(self.db)
        cache_key = sha256_text(
            compact_json(
                {
                    "query": query,
                    "domains": selected_domains,
                    "start": effective_start,
                    "end": effective_end,
                    "person": person,
                    "limit": safe_limit,
                    "include_body_snippets": include_body_snippets,
                    "cursor": cursor_payload,
                    "plan": plan.__dict__,
                }
            )
        )
        freshness_meta = freshness_status(self.db, self.settings.stale_after_seconds)
        refresh_status = _refresh_status(freshness_policy, freshness_meta)
        if freshness_policy != "refresh_if_stale":
            cached = query_cache_get(self.db, cache_key, generation)
            if cached:
                cached["meta"] = {**cached.get("meta", {}), "cache": "hit", "index_generation": generation}
                return cached

        rows = search_documents(
            self.db,
            query=effective_query,
            domains=db_domains,
            limit=safe_limit,
            offset=offset,
            snippet_chars=self.settings.snippet_chars if include_body_snippets else 160,
            start=effective_start,
            end=effective_end,
            person=person or (planned_people[0] if planned_people else None),
        )
        response = {
            "content": _compact_content(rows),
            "structured_content": {"results": rows},
            "query": query,
            "normalized_query": normalize_text(" ".join(tokenize(query))),
            "query_plan": plan.__dict__,
            "filters": {
                "domains": selected_domains,
                "start": effective_start,
                "end": effective_end,
                "person": person,
                "freshness": freshness_policy,
            },
            "index_freshness": freshness(self.db),
            "freshness_status": freshness_meta,
            "answer_hints": answer_hints(query, rows, plan.intent),
            "results": rows,
            "next_cursor": next_cursor(
                offset, len(rows), safe_limit, self.settings.cursor_secret, {"index_generation": generation}
            ),
            "meta": {"cache": "miss", "index_generation": generation, "refresh": refresh_status},
        }
        query_cache_set(self.db, cache_key, response, generation, ttl_seconds=self.settings.query_cache_ttl_seconds)
        return response


def answer_hints(query: str, results: list[dict[str, Any]], intent: str = "general_search") -> list[dict[str, Any]]:
    """Generate deterministic compact hints from top search rows."""

    if not results:
        return []
    query_terms = set(tokenize(query))
    top = results[0]
    source_id = top.get("occurrence_id") or top["id"]
    if (
        top.get("domain") == "calendar"
        and (intent == "calendar_time_lookup" or {"time", "meeting"} & query_terms)
        and top.get("time")
    ):
        time = top["time"]
        return [
            {
                "type": "calendar_time",
                "confidence": min(float(top.get("score", 0.0)), 0.95),
                "text": f"Likely meeting: {top.get('title')} from {time.get('start')} to {time.get('end')} {time.get('timezone')}.",
                "source_ids": [source_id],
            }
        ]
    if top.get("domain") == "contacts":
        return [{"type": "contact_identity", "confidence": top.get("score", 0.0), "source_ids": [source_id]}]
    if top.get("domain") == "mail":
        return [{"type": "mail_evidence", "confidence": top.get("score", 0.0), "source_ids": [source_id]}]
    if len(results) > 1 and abs(float(results[0].get("score", 0.0)) - float(results[1].get("score", 0.0))) < 0.05:
        return [{"type": "ambiguous_candidates", "source_ids": [row["id"] for row in results[:3]]}]
    return []


def _external_domains(domains: list[str]) -> list[str]:
    return ["contacts" if domain == "contact" else domain for domain in domains]


def _refresh_status(freshness_policy: str, statuses: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if freshness_policy != "refresh_if_stale":
        return {"status": "not_requested"}
    stale = {domain: value for domain, value in statuses.items() if value.get("status") in {"stale", "never_synced"}}
    if not stale:
        return {"status": "fresh"}
    return {"status": "refresh_unavailable_inline", "stale_domains": sorted(stale), "reason": "background sync only"}


def _compact_content(rows: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"{index + 1}. [{row.get('domain')}] {row.get('title')}: {row.get('snippet')}" for index, row in enumerate(rows)
    )
