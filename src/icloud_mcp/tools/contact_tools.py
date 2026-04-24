"""FastMCP Contact tool registration."""

from __future__ import annotations

from icloud_mcp.config import Settings
from icloud_mcp.db.connection import Database
from icloud_mcp.db.repositories import list_contacts, search_contacts, view_contact
from icloud_mcp.tools.search_tools import READ_ANNOTATIONS
from icloud_mcp.util import cursor_error, decode_cursor


def register_contact_tools(mcp: object, db: Database, settings: Settings) -> None:
    """Register compact read-only contact tools."""

    @mcp.tool(name="icloud.contacts.list", annotations=READ_ANNOTATIONS)
    async def contacts_list(
        addressbook_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        """List compact contact rows from local cache."""

        try:
            cursor_payload = decode_cursor(cursor, settings.cursor_secret)
        except ValueError as exc:
            return cursor_error(exc)
        return list_contacts(
            db,
            addressbook_id=addressbook_id,
            limit=max(1, min(limit, 100)),
            offset=int(cursor_payload.get("offset", 0)),
            cursor_secret=settings.cursor_secret,
        )

    @mcp.tool(name="icloud.contacts.view", annotations=READ_ANNOTATIONS)
    async def contacts_view(contact_id: str, include_notes: bool = False) -> dict:
        """View one cached contact."""

        result = view_contact(db, contact_id=contact_id, include_notes=include_notes)
        return result or {"status": "not_found", "contact_id": contact_id}

    @mcp.tool(name="icloud.contacts.search", annotations=READ_ANNOTATIONS)
    async def contacts_search(query: str, limit: int = 10, cursor: str | None = None) -> dict:
        """Search local contacts using aliases."""

        try:
            cursor_payload = decode_cursor(cursor, settings.cursor_secret)
        except ValueError as exc:
            return cursor_error(exc)
        return search_contacts(
            db,
            query=query,
            limit=max(1, min(limit, 50)),
            offset=int(cursor_payload.get("offset", 0)),
            cursor_secret=settings.cursor_secret,
        )
