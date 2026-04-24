"""FastMCP Mail tool registration."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import Field

from icloud_mcp.config import Settings
from icloud_mcp.db.connection import Database
from icloud_mcp.db.repositories import list_mail, view_mail
from icloud_mcp.tools.search_tools import READ_ANNOTATIONS
from icloud_mcp.util import cursor_error, decode_cursor


def register_mail_tools(mcp: object, db: Database, settings: Settings) -> None:
    """Register compact read-only mail tools."""

    @mcp.tool(name="icloud.mail.list", annotations=READ_ANNOTATIONS)
    async def mail_list(
        mailbox: str = "INBOX",
        after: datetime | None = None,
        before: datetime | None = None,
        from_email: Annotated[str | None, Field(alias="from")] = None,
        limit: int = 25,
        cursor: str | None = None,
    ) -> dict:
        """List compact mail rows from local cache."""

        try:
            cursor_payload = decode_cursor(cursor, settings.cursor_secret)
        except ValueError as exc:
            return cursor_error(exc)
        return list_mail(
            db,
            mailbox=mailbox,
            after=after.isoformat() if after else None,
            before=before.isoformat() if before else None,
            sender=from_email,
            limit=max(1, min(limit, 100)),
            offset=int(cursor_payload.get("offset", 0)),
            cursor_secret=settings.cursor_secret,
        )

    @mcp.tool(name="icloud.mail.view", annotations=READ_ANNOTATIONS)
    async def mail_view(
        message_id: str,
        include: list[str] | None = None,
        max_body_chars: int | None = None,
        body_offset: int = 0,
    ) -> dict:
        """View one cached mail message."""

        requested = include or ["headers", "body_text"]
        result = view_mail(
            db,
            message_id=message_id,
            include=requested,
            max_body_chars=max(1, min(max_body_chars or settings.mail_body_view_chars, 20000)),
            body_offset=max(0, body_offset),
        )
        return result or {"status": "not_found", "message_id": message_id}
