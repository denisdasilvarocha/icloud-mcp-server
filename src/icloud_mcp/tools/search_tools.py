"""FastMCP unified search tool registration."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from icloud_mcp.config import Settings
from icloud_mcp.db.connection import Database
from icloud_mcp.services.search import SearchService
from icloud_mcp.util import decode_cursor

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

        cursor_payload = decode_cursor(cursor, settings.cursor_secret)
        return SearchService(db, settings).search(
            query=query,
            domains=list(domains) if domains else None,
            start=start,
            end=end,
            person=person,
            limit=limit,
            include_body_snippets=include_body_snippets,
            freshness_policy=freshness_policy,
            cursor_payload=cursor_payload,
        )

    @mcp.tool(name="icloud.mail.search", annotations=READ_ANNOTATIONS)
    async def mail_search(
        query: str,
        start: datetime | None = None,
        end: datetime | None = None,
        person: str | None = None,
        include_body_snippets: bool = True,
        freshness_policy: Literal["cache_only", "allow_stale", "refresh_if_stale"] = "allow_stale",
        limit: int = 10,
        cursor: str | None = None,
    ) -> dict:
        """Search only local Mail cache."""

        return await search(
            query=query,
            domains=["mail"],
            start=start,
            end=end,
            person=person,
            include_body_snippets=include_body_snippets,
            freshness_policy=freshness_policy,
            limit=limit,
            cursor=cursor,
        )

    @mcp.tool(name="icloud.calendar.search_events", annotations=READ_ANNOTATIONS)
    async def calendar_search_events(
        query: str,
        start: datetime | None = None,
        end: datetime | None = None,
        person: str | None = None,
        freshness_policy: Literal["cache_only", "allow_stale", "refresh_if_stale"] = "allow_stale",
        limit: int = 10,
        cursor: str | None = None,
    ) -> dict:
        """Search only local Calendar cache."""

        return await search(
            query=query,
            domains=["calendar"],
            start=start,
            end=end,
            person=person,
            freshness_policy=freshness_policy,
            limit=limit,
            cursor=cursor,
        )
