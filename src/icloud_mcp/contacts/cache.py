"""Contacts repository interface."""

from __future__ import annotations

from typing import Any

from icloud_mcp.platform.util import compact_json, next_cursor, normalize_text, parse_json, utc_now
from icloud_mcp.search.repository import upsert_search_document
from icloud_mcp.storage.cache_state import bump_index_generation
from icloud_mcp.storage.connection import Database


def tombstone_contact(db: Database, contact_id: str) -> None:
    """Mark a contact deleted and cleanup aliases/search rows."""

    now = utc_now()
    db.execute("UPDATE contacts SET deleted_at = ? WHERE id = ?", (now, contact_id))
    db.execute("DELETE FROM person_aliases WHERE contact_id = ?", (contact_id,))
    db.execute("DELETE FROM contact_trigram_fts WHERE contact_id = ?", (contact_id,))
    db.execute(
        "UPDATE search_documents SET deleted_at = ? WHERE object_id = ? AND domain = 'contact'", (now, contact_id)
    )
    db.execute("DELETE FROM search_fts WHERE object_id = ? AND domain = 'contact'", (contact_id,))
    bump_index_generation(db)


def list_contacts(
    db: Database,
    addressbook_id: str | None,
    limit: int,
    offset: int,
    cursor_secret: str,
) -> dict[str, Any]:
    """List compact contacts."""

    filters = ["deleted_at IS NULL"]
    parameters: list[Any] = []
    if addressbook_id:
        filters.append("addressbook_id = ?")
        parameters.append(addressbook_id)
    rows = db.query(
        f"""
        SELECT id, display_name, emails_json, phones_json, organization
        FROM contacts
        WHERE {" AND ".join(filters)}
        ORDER BY display_name
        LIMIT ? OFFSET ?
        """,
        (*parameters, limit + 1, offset),
    )
    has_more = len(rows) > limit
    contacts = [_contact_summary(row) for row in rows[:limit]]
    return {
        "contacts": contacts,
        "next_cursor": next_cursor(offset, len(contacts), limit, cursor_secret, has_more=has_more),
    }


def upsert_addressbook(
    db: Database,
    *,
    account_id: str,
    addressbook_id: str,
    url: str,
    display_name: str,
    sync_token: str | None = None,
    ctag: str | None = None,
    last_sync_at: str | None = None,
) -> None:
    """Upsert a CardDAV addressbook collection."""

    db.execute(
        """
        INSERT INTO addressbooks (id, account_id, url, display_name, sync_token, ctag, last_sync_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
          id = excluded.id,
          display_name = excluded.display_name,
          sync_token = COALESCE(excluded.sync_token, addressbooks.sync_token),
          ctag = COALESCE(excluded.ctag, addressbooks.ctag),
          last_sync_at = COALESCE(excluded.last_sync_at, addressbooks.last_sync_at)
        """,
        (addressbook_id, account_id, url, display_name, sync_token, ctag, last_sync_at),
    )


def view_contact(db: Database, contact_id: str, include_notes: bool) -> dict[str, Any] | None:
    """Return one contact."""

    row = db.query_one("SELECT * FROM contacts WHERE id = ? AND deleted_at IS NULL", (contact_id,))
    if not row:
        return None
    contact = _contact_summary(row)
    contact["given_name"] = row["given_name"]
    contact["family_name"] = row["family_name"]
    if include_notes:
        contact["notes"] = row["notes"]
    contact["content_trust"] = "untrusted_user_data"
    return contact


def upsert_contact(
    db: Database,
    *,
    addressbook_id: str,
    contact_id: str,
    href: str,
    raw_vcard: str,
    display_name: str,
    emails: list[str],
    phones: list[str] | None = None,
    given_name: str | None = None,
    family_name: str | None = None,
    organization: str | None = None,
    notes: str | None = None,
    extra_aliases: list[tuple[str, str, float]] | None = None,
) -> None:
    """Upsert a synced contact, aliases, trigram row, and search document."""

    now = utc_now()
    raw_phones = phones or []
    normalized_phones = [_normalize_phone_alias(phone) for phone in raw_phones]
    phone_aliases = [phone for phone in normalized_phones if phone]
    db.execute(
        """
        INSERT INTO contacts
          (id, addressbook_id, href, raw_vcard, display_name, given_name, family_name,
           emails_json, phones_json, organization, notes, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(addressbook_id, href) DO UPDATE SET
          id = excluded.id,
          raw_vcard = excluded.raw_vcard,
          display_name = excluded.display_name,
          given_name = excluded.given_name,
          family_name = excluded.family_name,
          emails_json = excluded.emails_json,
          phones_json = excluded.phones_json,
          organization = excluded.organization,
          notes = excluded.notes,
          updated_at = excluded.updated_at,
          deleted_at = NULL
        """,
        (
            contact_id,
            addressbook_id,
            href,
            raw_vcard,
            display_name,
            given_name,
            family_name,
            compact_json(emails),
            compact_json(raw_phones),
            organization,
            notes,
            now,
        ),
    )
    db.execute("DELETE FROM person_aliases WHERE contact_id = ?", (contact_id,))
    aliases = _contact_aliases(display_name, emails, given_name, family_name, organization)
    typed_aliases = [
        (alias, "email" if "@" in alias else "name", 0.95 if alias == display_name else 0.85) for alias in aliases
    ]
    for email in emails:
        local_part = email.split("@", 1)[0]
        if local_part:
            typed_aliases.append((local_part, "email_local_part", 0.7))
    typed_aliases.extend((phone, "phone_e164", 0.75) for phone in phone_aliases)
    typed_aliases.extend(extra_aliases or [])
    db.executemany(
        """
        INSERT OR REPLACE INTO person_aliases (alias, normalized_alias, contact_id, alias_type, confidence)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (alias, normalize_text(alias), contact_id, alias_type, confidence)
            for alias, alias_type, confidence in typed_aliases
        ],
    )
    db.execute("DELETE FROM contact_trigram_fts WHERE contact_id = ?", (contact_id,))
    db.execute(
        """
        INSERT INTO contact_trigram_fts (contact_id, display_name, emails)
        VALUES (?, ?, ?)
        """,
        (contact_id, display_name, " ".join([*emails, *raw_phones, *phone_aliases])),
    )
    upsert_search_document(
        db,
        document_id=f"doc_{contact_id}",
        domain="contact",
        object_id=contact_id,
        title=display_name,
        text="\n".join(
            part
            for part in [
                f"Name: {display_name}",
                f"Emails: {', '.join(emails)}",
                f"Phones: {', '.join(raw_phones)}",
                f"Organization: {organization or ''}",
            ]
            if part
        ),
        metadata={"emails": emails, "phones": raw_phones, "phone_aliases": phone_aliases, "organization": organization},
        participants=" ".join(aliases),
    )


def _normalize_phone_alias(phone: str) -> str | None:
    digits = "".join(char for char in phone if char.isdigit())
    if not digits:
        return None
    if phone.strip().startswith("+"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return digits


def search_contacts(
    db: Database,
    query: str,
    limit: int,
    offset: int = 0,
    cursor_secret: str | None = None,
) -> dict[str, Any]:
    """Search contacts through alias and trigram tables."""

    normalized = normalize_text(query)
    trigram_query = '"' + query.replace('"', '""') + '"'
    trigram_rows = db.query(
        """
        SELECT c.id, c.display_name, c.emails_json, c.phones_json, c.organization, 0.72 AS confidence
        FROM contact_trigram_fts f
        JOIN contacts c ON c.id = f.contact_id
        WHERE contact_trigram_fts MATCH ? AND c.deleted_at IS NULL
        LIMIT ? OFFSET ?
        """,
        (trigram_query, limit + 1, offset),
    )
    rows = db.query(
        """
        SELECT c.id, c.display_name, c.emails_json, c.phones_json, c.organization, MAX(pa.confidence) AS confidence
        FROM contacts c
        LEFT JOIN person_aliases pa ON pa.contact_id = c.id
        WHERE c.deleted_at IS NULL
          AND (
            pa.normalized_alias LIKE ?
            OR c.display_name LIKE ?
            OR c.emails_json LIKE ?
          )
        GROUP BY c.id, c.display_name, c.emails_json, c.phones_json, c.organization
        ORDER BY COALESCE(MAX(pa.confidence), 0.5) DESC, c.display_name
        LIMIT ? OFFSET ?
        """,
        (f"%{normalized}%", f"%{query}%", f"%{query}%", limit + 1, offset),
    )
    merged: dict[str, dict[str, Any]] = {}
    for row in [*rows, *trigram_rows]:
        existing = merged.get(row["id"])
        if existing and float(existing.get("confidence") or 0) >= float(row.get("confidence") or 0):
            continue
        merged[row["id"]] = row
    contacts = []
    sorted_rows = sorted(
        merged.values(), key=lambda item: (-(float(item.get("confidence") or 0.5)), item["display_name"])
    )
    has_more = len(sorted_rows) > limit or len(rows) > limit or len(trigram_rows) > limit
    for row in sorted_rows[:limit]:
        contact = _contact_summary(row)
        contact["score"] = round(float(row.get("confidence") or 0.5), 3)
        contacts.append(contact)
    response = {"contacts": contacts}
    if cursor_secret:
        response["next_cursor"] = next_cursor(offset, len(contacts), limit, cursor_secret, has_more=has_more)
    return response


def _contact_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "display_name": row["display_name"],
        "emails": parse_json(row.get("emails_json"), []),
        "phones": parse_json(row.get("phones_json"), []),
        "organization": row.get("organization"),
    }


def _contact_aliases(
    display_name: str,
    emails: list[str],
    given_name: str | None,
    family_name: str | None,
    organization: str | None,
) -> list[str]:
    aliases = [display_name, *emails]
    if given_name:
        aliases.append(given_name)
    if family_name:
        aliases.append(family_name)
    if organization:
        aliases.append(organization)
    return [alias for alias in dict.fromkeys(alias.strip() for alias in aliases) if alias]
