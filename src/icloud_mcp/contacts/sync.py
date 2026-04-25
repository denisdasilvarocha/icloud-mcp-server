"""Contacts sync worker."""

from __future__ import annotations

from dataclasses import dataclass, replace
from urllib.parse import urljoin

from icloud_mcp.contacts.adapter import CardDAVContactsAdapter
from icloud_mcp.contacts.cache import tombstone_contact, upsert_addressbook, upsert_contact
from icloud_mcp.platform.config import Settings
from icloud_mcp.platform.secrets import load_icloud_credentials
from icloud_mcp.platform.util import utc_now
from icloud_mcp.storage.connection import Database
from icloud_mcp.sync.capabilities import ContactsDeltaAdapter, supports_contacts_delta
from icloud_mcp.sync.checkpoints import update_checkpoint, update_failure_checkpoint
from icloud_mcp.sync.delta import sync_delta_first


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
        result = sync_delta_first(
            db=self.db,
            collections=addressbooks,
            existing_sql="SELECT sync_token, ctag FROM addressbooks WHERE url = ?",
            sync_changes=lambda addressbook, sync_token: adapter.sync_contact_changes(
                apple_id=apple_id,
                app_password=app_password,
                addressbook=addressbook,
                sync_token=sync_token,
            ),
            full_sync_items=lambda: adapter.sync_contacts(apple_id=apple_id, app_password=app_password)[1],
            item_collection_id=lambda contact: contact.addressbook_id,
            deleted_href=lambda addressbook, href: _absolute_member_url(addressbook.url, href),
            collection_with_sync_token=lambda addressbook, sync_token: replace(addressbook, sync_token=sync_token),
        )
        return result.collections, result.items, result.full_sync_collection_ids, result.deleted_hrefs


def _absolute_member_url(collection_url: str, href: str) -> str:
    return urljoin(collection_url, href)
