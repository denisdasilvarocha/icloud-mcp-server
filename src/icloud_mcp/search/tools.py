"""FastMCP unified search tool registration."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastmcp.tools import ToolResult

from icloud_mcp.mcp.boundary import cursor_state_or_error, search_tool_result, tool_error_result
from icloud_mcp.platform.config import Settings
from icloud_mcp.search.service import SearchService
from icloud_mcp.storage.connection import Database

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
        freshness: Literal["cache_only", "allow_stale", "refresh_if_stale"] = "allow_stale",
        cursor: str | None = None,
    ) -> ToolResult:
        """Search local iCloud Mail, Calendar, and Contacts cache."""

        cursor_payload, error = cursor_state_or_error(cursor, settings.cursor_secret)
        if error:
            return tool_error_result(error)
        result = SearchService(db, settings).search(
            query=query,
            domains=list(domains) if domains else None,
            start=start,
            end=end,
            person=person,
            limit=limit,
            include_body_snippets=include_body_snippets,
            freshness_policy=freshness,
            cursor_payload=cursor_payload,
        )
        return search_tool_result(result)

    @mcp.tool(name="icloud.mail.search", annotations=READ_ANNOTATIONS)
    async def mail_search(
        query: str,
        start: datetime | None = None,
        end: datetime | None = None,
        person: str | None = None,
        include_body_snippets: bool = True,
        freshness: Literal["cache_only", "allow_stale", "refresh_if_stale"] = "allow_stale",
        limit: int = 10,
        cursor: str | None = None,
    ) -> ToolResult:
        """Search only local Mail cache."""

        return await search(
            query=query,
            domains=["mail"],
            start=start,
            end=end,
            person=person,
            include_body_snippets=include_body_snippets,
            freshness=freshness,
            limit=limit,
            cursor=cursor,
        )

    @mcp.tool(name="icloud.calendar.search_events", annotations=READ_ANNOTATIONS)
    async def calendar_search_events(
        query: str,
        start: datetime | None = None,
        end: datetime | None = None,
        person: str | None = None,
        freshness: Literal["cache_only", "allow_stale", "refresh_if_stale"] = "allow_stale",
        limit: int = 10,
        cursor: str | None = None,
    ) -> ToolResult:
        """Search only local Calendar cache."""

        return await search(
            query=query,
            domains=["calendar"],
            start=start,
            end=end,
            person=person,
            freshness=freshness,
            limit=limit,
            cursor=cursor,
        )
