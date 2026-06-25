"""Search service coordinating query planning, cache policy, and repositories."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from icloud_mcp.platform.config import Settings
from icloud_mcp.platform.util import next_cursor, tokenize
from icloud_mcp.search.policy import _external_domains, _refresh_status, resolve_search_policy
from icloud_mcp.search.repository import person_alias_terms, search_documents
from icloud_mcp.storage.cache_state import freshness, freshness_status, index_generation
from icloud_mcp.storage.connection import Database

__all__ = ["SearchService", "answer_hints", "_external_domains", "_refresh_status"]


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
        policy = resolve_search_policy(
            query=query,
            domains=domains,
            start=start,
            end=end,
            person=person,
            limit=limit,
            include_body_snippets=include_body_snippets,
            cursor_payload=cursor_payload,
            snippet_chars=self.settings.snippet_chars,
            alias_resolver=lambda planned_person: person_alias_terms(self.db, planned_person),
        )
        generation = index_generation(self.db)
        freshness_meta = freshness_status(self.db, self.settings.stale_after_seconds)
        refresh_status = _refresh_status(freshness_policy, freshness_meta)
        rows = search_documents(
            self.db,
            query=policy.effective_query,
            domains=policy.db_domains,
            limit=policy.safe_limit + 1,
            offset=policy.offset,
            snippet_chars=policy.snippet_chars,
            start=policy.effective_start,
            end=policy.effective_end,
            person=policy.person_filter,
        )
        has_more = len(rows) > policy.safe_limit
        rows = rows[: policy.safe_limit]
        response = {
            "content": _compact_content(rows),
            "structured_content": {"results": rows},
            "query": query,
            "normalized_query": policy.normalized_query,
            "query_plan": policy.plan.__dict__,
            "filters": {
                "domains": policy.selected_domains,
                "start": policy.effective_start,
                "end": policy.effective_end,
                "person": person,
                "freshness": freshness_policy,
            },
            "index_freshness": freshness(self.db),
            "freshness_status": freshness_meta,
            "answer_hints": answer_hints(query, rows, policy.plan.intent),
            "results": rows,
            "next_cursor": next_cursor(
                policy.offset,
                len(rows),
                policy.safe_limit,
                self.settings.cursor_secret,
                {"index_generation": generation},
                has_more=has_more,
            ),
            "meta": {"cache": "not_used", "index_generation": generation, "refresh": refresh_status},
        }
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


def _compact_content(rows: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"{index + 1}. [{row.get('domain')}] {row.get('title')}: {row.get('snippet')}" for index, row in enumerate(rows)
    )
