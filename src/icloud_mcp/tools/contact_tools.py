"""FastMCP Contact tool registration."""

from __future__ import annotations

from icloud_mcp.config import Settings
from icloud_mcp.db.connection import Database
from icloud_mcp.db.contacts_repository import list_contacts, search_contacts, view_contact
from icloud_mcp.tools.boundary import bounded_int, cursor_offset, decode_cursor_or_error, not_found
from icloud_mcp.tools.search_tools import READ_ANNOTATIONS


def register_contact_tools(mcp: object, db: Database, settings: Settings) -> None:
    """Register compact read-only contact tools."""

    @mcp.tool(name="icloud.contacts.list", annotations=READ_ANNOTATIONS)
    async def contacts_list(
        addressbook_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        """List compact contact rows from local cache."""

        cursor_payload, error = decode_cursor_or_error(cursor, settings.cursor_secret)
        if error:
            return error
        return list_contacts(
            db,
            addressbook_id=addressbook_id,
            limit=bounded_int(limit, minimum=1, maximum=100),
            offset=cursor_offset(cursor_payload),
            cursor_secret=settings.cursor_secret,
        )

    @mcp.tool(name="icloud.contacts.view", annotations=READ_ANNOTATIONS)
    async def contacts_view(contact_id: str, include_notes: bool = False) -> dict:
        """View one cached contact."""

        result = view_contact(db, contact_id=contact_id, include_notes=include_notes)
        return result or not_found("contact_id", contact_id)

    @mcp.tool(name="icloud.contacts.search", annotations=READ_ANNOTATIONS)
    async def contacts_search(query: str, limit: int = 10, cursor: str | None = None) -> dict:
        """Search local contacts using aliases."""

        cursor_payload, error = decode_cursor_or_error(cursor, settings.cursor_secret)
        if error:
            return error
        return search_contacts(
            db,
            query=query,
            limit=bounded_int(limit, minimum=1, maximum=50),
            offset=cursor_offset(cursor_payload),
            cursor_secret=settings.cursor_secret,
        )
