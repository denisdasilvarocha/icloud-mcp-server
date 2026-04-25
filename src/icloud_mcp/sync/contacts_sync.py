"""Contacts sync worker."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin

from icloud_mcp.adapters.carddav_contacts import CardDAVContactsAdapter
from icloud_mcp.config import Settings
from icloud_mcp.db.connection import Database
from icloud_mcp.db.contacts_repository import tombstone_contact, upsert_addressbook, upsert_contact
from icloud_mcp.security.secrets import load_icloud_credentials
from icloud_mcp.sync.capabilities import ContactsDeltaAdapter, supports_contacts_delta
from icloud_mcp.sync.checkpoints import update_checkpoint, update_failure_checkpoint
from icloud_mcp.util import utc_now


@dataclass
class ContactsSyncWorker:
    """Synchronize iCloud Contacts through CardDAV."""

    db: Database
    settings: Settings
    adapter: CardDAVContactsAdapter | None = None

    name = "contacts_sync_worker"

    def run_once(self) -> dict:
        """Run one contacts sync cycle."""

        credentials = load_icloud_credentials(self.settings)
        if not credentials:
            result = {"status": "skipped", "reason": "credentials_missing"}
            update_checkpoint(self.db, self.name, "skipped", result)
            return result

        try:
            adapter = self.adapter or CardDAVContactsAdapter()
            if supports_contacts_delta(adapter):
                addressbooks, contacts, full_sync_books, deleted_hrefs = self._sync_with_tokens(
                    adapter, credentials.apple_id, credentials.app_password
                )
            else:
                addressbooks, contacts = adapter.sync_contacts(
                    apple_id=credentials.apple_id,
                    app_password=credentials.app_password,
                )
                full_sync_books = {addressbook.id for addressbook in addressbooks}
                deleted_hrefs = []
            now = utc_now()
            for addressbook in addressbooks:
                upsert_addressbook(
                    self.db,
                    account_id=self.settings.default_account_id,
                    addressbook_id=addressbook.id,
                    url=addressbook.url,
                    display_name=addressbook.display_name,
                    sync_token=addressbook.sync_token,
                    ctag=addressbook.ctag,
                    last_sync_at=now,
                )
            for contact in contacts:
                upsert_contact(
                    self.db,
                    addressbook_id=contact.addressbook_id,
                    contact_id=contact.id,
                    href=contact.href,
                    raw_vcard=contact.raw_vcard,
                    display_name=contact.display_name,
                    emails=contact.emails,
                    phones=contact.phones,
                    given_name=contact.given_name,
                    family_name=contact.family_name,
                    organization=contact.organization,
                    notes=contact.notes,
                    extra_aliases=contact.extra_aliases,
                )
            for href in deleted_hrefs:
                row = self.db.query_one("SELECT id FROM contacts WHERE href = ? AND deleted_at IS NULL", (href,))
                if row:
                    tombstone_contact(self.db, row["id"])
            synced_by_book: dict[str, set[str]] = {}
            for contact in contacts:
                if contact.addressbook_id in full_sync_books:
                    synced_by_book.setdefault(contact.addressbook_id, set()).add(contact.id)
            for addressbook_id, synced_ids in synced_by_book.items():
                existing = self.db.query(
                    "SELECT id FROM contacts WHERE addressbook_id = ? AND deleted_at IS NULL", (addressbook_id,)
                )
                for row in existing:
                    if row["id"] not in synced_ids:
                        tombstone_contact(self.db, row["id"])
            result = {"status": "ok", "addressbooks": len(addressbooks), "contacts": len(contacts)}
            update_checkpoint(self.db, self.name, "ok", result)
            return result
        except Exception as exc:
            return update_failure_checkpoint(
                self.db,
                self.name,
                exc,
                allow_unredacted=self.settings.allow_unredacted_debug,
            )

    def _sync_with_tokens(
        self,
        adapter: ContactsDeltaAdapter,
        apple_id: str,
        app_password: str,
    ) -> tuple[list, list, set[str], list[str]]:
        addressbooks = adapter.discover_addressbooks(apple_id=apple_id, app_password=app_password)
        synced_addressbooks = []
        contacts = []
        deleted_hrefs: list[str] = []
        full_sync_books: set[str] = set()
        fallback_needed = False
        for addressbook in addressbooks:
            existing = self.db.query_one("SELECT sync_token, ctag FROM addressbooks WHERE url = ?", (addressbook.url,))
            if existing and existing.get("sync_token") and addressbook.sync_token:
                try:
                    result, changed = adapter.sync_contact_changes(
                        apple_id=apple_id,
                        app_password=app_password,
                        addressbook=addressbook,
                        sync_token=existing["sync_token"],
                    )
                except Exception:
                    synced_addressbooks.append(addressbook)
                    fallback_needed = True
                    full_sync_books.add(addressbook.id)
                    continue
                contacts.extend(changed)
                deleted_hrefs.extend([_absolute_member_url(addressbook.url, href) for href in result.deleted])
                if not result.sync_token:
                    fallback_needed = True
                    full_sync_books.add(addressbook.id)
                    synced_addressbooks.append(addressbook)
                    continue
                synced_addressbooks.append(
                    type(addressbook)(
                        id=addressbook.id,
                        url=addressbook.url,
                        display_name=addressbook.display_name,
                        sync_token=result.sync_token,
                        ctag=addressbook.ctag,
                    )
                )
                continue
            synced_addressbooks.append(addressbook)
            if not existing or not addressbook.ctag or existing.get("ctag") != addressbook.ctag:
                fallback_needed = True
                full_sync_books.add(addressbook.id)
        if fallback_needed:
            _, full_contacts = adapter.sync_contacts(apple_id=apple_id, app_password=app_password)
            contacts.extend(contact for contact in full_contacts if contact.addressbook_id in full_sync_books)
        return synced_addressbooks, contacts, full_sync_books, deleted_hrefs


def _absolute_member_url(collection_url: str, href: str) -> str:
    return urljoin(collection_url, href)
