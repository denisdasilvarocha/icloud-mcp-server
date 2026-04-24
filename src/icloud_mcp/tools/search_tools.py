"""FastMCP unified search tool registration."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from icloud_mcp.config import Settings
from icloud_mcp.db.connection import Database
from icloud_mcp.db.repositories import freshness, index_generation, search_documents
from icloud_mcp.util import decode_cursor, next_cursor, normalize_text, tokenize

READ_ANNOTATIONS = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}


def register_search_tools(mcp: object, db: Database, settings: Settings) -> None:
    """Register unified local RAG search."""

    @mcp.tool(name="icloud.search", annotations=READ_ANNOTATIONS)
    async def search(
        query: str,
        domains: list[Literal["mail", "calendar", "contacts"]] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        person: str | None = None,
        limit: int = 10,
        include_body_snippets: bool = True,
        freshness_policy: Literal["cache_only", "allow_stale", "refresh_if_stale"] = "allow_stale",
        cursor: str | None = None,
    ) -> dict:
        """Search local iCloud Mail, Calendar, and Contacts cache."""

        selected_domains = list(domains or ["mail", "calendar", "contacts"])
        db_domains = ["contact" if domain == "contacts" else domain for domain in selected_domains]
        cursor_payload = decode_cursor(cursor, settings.cursor_secret)
        offset = int(cursor_payload.get("offset", 0))
        safe_limit = max(1, min(limit, 50))
        effective_query = " ".join(part for part in [query, person or ""] if part).strip()
        rows = search_documents(
            db,
            query=effective_query,
            domains=db_domains,
            limit=safe_limit,
            offset=offset,
            snippet_chars=settings.snippet_chars if include_body_snippets else 160,
        )

        hints = _answer_hints(query, rows)
        return {
            "query": query,
            "normalized_query": normalize_text(" ".join(tokenize(query))),
            "filters": {
                "domains": selected_domains,
                "start": start.isoformat() if start else None,
                "end": end.isoformat() if end else None,
                "person": person,
                "freshness": freshness_policy,
            },
            "index_freshness": freshness(db),
            "answer_hints": hints,
            "results": rows,
            "next_cursor": next_cursor(
                offset,
                len(rows),
                safe_limit,
                settings.cursor_secret,
                {"index_generation": index_generation(db)},
            ),
        }

    @mcp.tool(name="icloud.mail.search", annotations=READ_ANNOTATIONS)
    async def mail_search(query: str, limit: int = 10, cursor: str | None = None) -> dict:
        """Search only local Mail cache."""

        return await search(query=query, domains=["mail"], limit=limit, cursor=cursor)

    @mcp.tool(name="icloud.calendar.search_events", annotations=READ_ANNOTATIONS)
    async def calendar_search_events(query: str, limit: int = 10, cursor: str | None = None) -> dict:
        """Search only local Calendar cache."""

        return await search(query=query, domains=["calendar"], limit=limit, cursor=cursor)


def _answer_hints(query: str, results: list[dict]) -> list[dict]:
    """Generate deterministic compact hints from top search rows."""

    if not results:
        return []
    query_terms = set(tokenize(query))
    top = results[0]
    if top.get("domain") == "calendar" and {"time", "meeting"} & query_terms and top.get("time"):
        time = top["time"]
        return [
            {
                "type": "calendar_time",
                "confidence": min(float(top.get("score", 0.0)), 0.95),
                "text": f"Likely meeting: {top.get('title')} from {time.get('start')} to {time.get('end')} {time.get('timezone')}.",
                "source_ids": [top["id"]],
            }
        ]
    return []
