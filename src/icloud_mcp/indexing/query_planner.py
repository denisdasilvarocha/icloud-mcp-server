"""Search query planning."""

from __future__ import annotations

from dataclasses import dataclass

from icloud_mcp.util import normalize_text, tokenize


@dataclass(frozen=True)
class QueryPlan:
    """Compact query plan."""

    raw: str
    normalized: str
    tokens: list[str]
    intent: str


def plan_query(query: str) -> QueryPlan:
    """Infer a simple deterministic query plan."""

    tokens = tokenize(query)
    intent = "calendar_time_lookup" if {"meeting", "time"} & set(tokens) else "general_search"
    return QueryPlan(raw=query, normalized=normalize_text(" ".join(tokens)), tokens=tokens, intent=intent)
