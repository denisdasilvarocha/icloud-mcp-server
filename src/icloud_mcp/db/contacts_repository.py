"""Contacts repository interface."""

from __future__ import annotations

from icloud_mcp.db.repositories import list_contacts as list_contacts
from icloud_mcp.db.repositories import search_contacts as search_contacts
from icloud_mcp.db.repositories import tombstone_contact as tombstone_contact
from icloud_mcp.db.repositories import upsert_addressbook as upsert_addressbook
from icloud_mcp.db.repositories import upsert_contact as upsert_contact
from icloud_mcp.db.repositories import view_contact as view_contact

__all__ = [
    "list_contacts",
    "search_contacts",
    "tombstone_contact",
    "upsert_addressbook",
    "upsert_contact",
    "view_contact",
]
