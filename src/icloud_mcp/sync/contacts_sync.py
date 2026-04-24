"""Contacts sync worker."""

from __future__ import annotations

from dataclasses import dataclass

from icloud_mcp.adapters.carddav_contacts import CardDAVContactsAdapter
from icloud_mcp.config import Settings
from icloud_mcp.db.connection import Database
from icloud_mcp.db.repositories import upsert_addressbook, upsert_contact
from icloud_mcp.sync.checkpoints import update_checkpoint
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

        if not self.settings.apple_id or not self.settings.app_password:
            result = {"status": "skipped", "reason": "credentials_missing"}
            update_checkpoint(self.db, self.name, "skipped", result)
            return result

        adapter = self.adapter or CardDAVContactsAdapter()
        addressbooks, contacts = adapter.sync_contacts(
            apple_id=self.settings.apple_id,
            app_password=self.settings.app_password,
        )
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
            )
        result = {"status": "ok", "addressbooks": len(addressbooks), "contacts": len(contacts)}
        update_checkpoint(self.db, self.name, "ok", result)
        return result
