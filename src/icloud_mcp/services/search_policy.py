"""Deterministic search policy decisions."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from icloud_mcp.indexing.query_planner import QueryPlan, plan_query
from icloud_mcp.util import compact_json, sha256_text


@dataclass(frozen=True)
class SearchPolicy:
    """Resolved local-cache search policy for one tool call."""

    query: str
    normalized_query: str
    plan: QueryPlan
    selected_domains: list[str]
    db_domains: list[str]
    safe_limit: int
    offset: int
    planned_people: list[str]
    person_filter: str | None
    effective_query: str
    effective_start: str | None
    effective_end: str | None
    snippet_chars: int
    cache_key: str


def resolve_search_policy(
    *,
    query: str,
    domains: list[str] | None,
    start: datetime | None,
    end: datetime | None,
    person: str | None,
    limit: int,
    include_body_snippets: bool,
    cursor_payload: dict[str, Any],
    snippet_chars: int,
    alias_resolver: Callable[[str], Iterable[str]],
) -> SearchPolicy:
    """Resolve planner, filters, bounds, and cache identity for local search."""

    plan = plan_query(query)
    selected_domains = list(domains) if domains else _external_domains(plan.domains)
    planned_people = [person] if person else plan.people or []
    person_terms = [
        alias
        for planned_person in planned_people
        for alias in alias_resolver(planned_person)
    ]
    effective_query = " ".join(part for part in [query, " ".join(person_terms)] if part).strip()
    effective_start = start.isoformat() if start else plan.start
    effective_end = end.isoformat() if end else plan.end
    safe_limit = max(1, min(limit, 50))
    policy = SearchPolicy(
        query=query,
        normalized_query=plan.normalized,
        plan=plan,
        selected_domains=selected_domains,
        db_domains=["contact" if domain == "contacts" else domain for domain in selected_domains],
        safe_limit=safe_limit,
        offset=int(cursor_payload.get("offset", 0)),
        planned_people=planned_people,
        person_filter=person or (planned_people[0] if planned_people else None),
        effective_query=effective_query,
        effective_start=effective_start,
        effective_end=effective_end,
        snippet_chars=snippet_chars if include_body_snippets else 160,
        cache_key="",
    )
    return SearchPolicy(
        **{
            **policy.__dict__,
            "cache_key": _cache_key(
                policy,
                person=person,
                include_body_snippets=include_body_snippets,
                cursor_payload=cursor_payload,
            ),
        }
    )


def _external_domains(domains: list[str]) -> list[str]:
    return ["contacts" if domain == "contact" else domain for domain in domains]


def _refresh_status(freshness_policy: str, statuses: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if freshness_policy != "refresh_if_stale":
        return {"status": "not_requested"}
    stale = {domain: value for domain, value in statuses.items() if value.get("status") in {"stale", "never_synced"}}
    if not stale:
        return {"status": "fresh"}
    return {"status": "refresh_unavailable_inline", "stale_domains": sorted(stale), "reason": "background sync only"}


def _cache_key(
    policy: SearchPolicy,
    *,
    person: str | None,
    include_body_snippets: bool,
    cursor_payload: dict[str, Any],
) -> str:
    cursor_offset = int(cursor_payload.get("offset", 0))
    return sha256_text(
        compact_json(
            {
                "version": 1,
                "query": policy.query,
                "normalized_query": policy.normalized_query,
                "domains": policy.selected_domains,
                "start": policy.effective_start,
                "end": policy.effective_end,
                "person": person,
                "person_filter": policy.person_filter,
                "limit": policy.safe_limit,
                "include_body_snippets": include_body_snippets,
                "cursor_offset": cursor_offset,
                "intent": policy.plan.intent,
                "planned_people": policy.planned_people,
            }
        )
    )
