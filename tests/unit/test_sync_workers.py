from __future__ import annotations

import unittest
from datetime import date

from icloud_mcp.adapters.caldav_calendar import (
    SyncedCalendar,
    SyncedCalendarEvent,
)
from icloud_mcp.adapters.caldav_calendar import (
    WebDAVSyncResult as CalendarSyncResult,
)
from icloud_mcp.adapters.carddav_contacts import (
    SyncedAddressBook,
    SyncedContact,
)
from icloud_mcp.adapters.carddav_contacts import (
    WebDAVSyncResult as ContactSyncResult,
)
from icloud_mcp.adapters.imap_mail import DeletedMailMessage, IMAPSyncDelta, SyncedMailbox, SyncedMailMessage
from icloud_mcp.config import Settings
from icloud_mcp.db.connection import open_db
from icloud_mcp.db.repositories import (
    ensure_defaults,
    search_documents,
    sync_status,
    upsert_addressbook,
    upsert_calendar_collection,
    upsert_calendar_object,
    upsert_contact,
)
from icloud_mcp.sync.calendar_sync import CalendarSyncWorker
from icloud_mcp.sync.contacts_sync import ContactsSyncWorker
from icloud_mcp.sync.mail_sync import MailBackfillWorker, MailSyncWorker
from icloud_mcp.sync.scheduler import SyncScheduler


class FakeMailAdapter:
    def sync_recent(self, **kwargs):
        return (
            [SyncedMailbox(id="mb_inbox", name="INBOX", uid_validity="1", uid_next=2, highest_modseq=None)],
            [
                SyncedMailMessage(
                    id="mail_msg_1",
                    mailbox_id="mb_inbox",
                    uid=1,
                    message_id="<1@example.com>",
                    subject="Contract deadline",
                    from_address={"name": "Liesa", "email": "liesa@example.com"},
                    to_addresses=[{"name": "Me", "email": "me@example.com"}],
                    cc_addresses=[],
                    date="2026-04-24T09:00:00+02:00",
                    flags=["\\Seen"],
                    size_bytes=100,
                    preview="Contract deadline",
                    body_text="Contract is due Friday.",
                    has_attachments=False,
                )
            ],
        )


class FakeMailBackfillAdapter:
    def sync_backfill(self, **kwargs):
        return (
            SyncedMailbox(
                id="mb_inbox",
                name=kwargs["mailbox"],
                uid_validity="1",
                uid_next=2,
                highest_modseq=None,
                last_synced_uid=0,
                backfill_cursor=None,
                backfill_status="complete",
            ),
            [
                SyncedMailMessage(
                    id="mail_msg_older",
                    mailbox_id="mb_inbox",
                    uid=0,
                    message_id="<older@example.com>",
                    subject="Older contract archive",
                    from_address={"name": "Liesa", "email": "liesa@example.com"},
                    to_addresses=[{"name": "Me", "email": "me@example.com"}],
                    cc_addresses=[],
                    date="2026-03-01T09:00:00+02:00",
                    flags=[],
                    size_bytes=100,
                    preview="Older contract archive",
                    body_text="Archived contract terms.",
                    has_attachments=False,
                )
            ],
        )


class FakeMailIncrementalAdapter:
    def sync_incremental(self, **kwargs):
        self.mailbox_states = kwargs["mailbox_states"]
        return IMAPSyncDelta(
            mailboxes=[
                SyncedMailbox(
                    id="mb_inbox",
                    name="INBOX",
                    uid_validity="1",
                    uid_next=4,
                    highest_modseq="9",
                    last_synced_uid=3,
                    backfill_cursor=None,
                    backfill_status="complete",
                )
            ],
            messages=[
                SyncedMailMessage(
                    id="mail_msg_3",
                    mailbox_id="mb_inbox",
                    uid=3,
                    message_id="<3@example.com>",
                    subject="New delta message",
                    from_address={"name": "Liesa", "email": "liesa@example.com"},
                    to_addresses=[{"name": "Me", "email": "me@example.com"}],
                    cc_addresses=[],
                    date="2026-04-25T09:00:00+02:00",
                    flags=[],
                    size_bytes=100,
                    preview="New delta message",
                    body_text="Incremental sync body.",
                    has_attachments=False,
                )
            ],
            deleted=[DeletedMailMessage(mailbox_id="mb_inbox", uid=1)],
        )


class FakeCalendarAdapter:
    def sync_events(self, **kwargs):
        self.start = kwargs["start"]
        self.end = kwargs["end"]
        self.assert_date_range()
        return (
            [
                SyncedCalendar(
                    id="cal_remote",
                    url="https://caldav.example/cal/",
                    display_name="Calendar",
                    color=None,
                    read_only=False,
                )
            ],
            [
                SyncedCalendarEvent(
                    id="cal_evt_1",
                    calendar_id="cal_remote",
                    href="https://caldav.example/cal/1.ics",
                    uid="event-1",
                    etag='"v1"',
                    raw_ics="BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:event-1\nSUMMARY:Project Sync\nEND:VEVENT\nEND:VCALENDAR",
                    summary="Project Sync",
                    description=None,
                    location="Zoom",
                    dtstart="2026-04-27T14:00:00+02:00",
                    dtend="2026-04-27T15:00:00+02:00",
                    timezone="Europe/Berlin",
                    attendees=[{"name": "Liesa", "email": "liesa@example.com"}],
                    organizer=None,
                    rrule=None,
                    recurrence_id=None,
                    status=None,
                )
            ],
        )

    def assert_date_range(self) -> None:
        assert isinstance(self.start, date)
        assert isinstance(self.end, date)


class FakeContactsAdapter:
    def sync_contacts(self, **kwargs):
        return (
            [
                SyncedAddressBook(
                    id="addr_remote",
                    url="https://contacts.example/addressbook/",
                    display_name="Contacts",
                    sync_token=None,
                    ctag=None,
                )
            ],
            [
                SyncedContact(
                    id="contact_1",
                    addressbook_id="addr_remote",
                    href="https://contacts.example/addressbook/1.vcf",
                    etag='"v1"',
                    uid="contact-1",
                    raw_vcard="BEGIN:VCARD\nFN:Liesa Müller\nEMAIL:liesa@example.com\nEND:VCARD",
                    display_name="Liesa Müller",
                    given_name="Liesa",
                    family_name="Müller",
                    emails=["liesa@example.com"],
                    phones=[],
                    organization=None,
                    notes=None,
                )
            ],
        )


class FakeCalendarDeltaAdapter:
    def discover(self, **kwargs):
        return [
            SyncedCalendar(
                id="cal_remote",
                url="https://caldav.example/cal/",
                display_name="Calendar",
                color=None,
                read_only=False,
                sync_token="token-new",
                ctag="ctag-new",
            )
        ]

    def sync_event_changes(self, **kwargs):
        return (
            CalendarSyncResult(sync_token="token-new", changed=[], deleted=["2.ics"]),
            [],
        )

    def sync_events(self, **kwargs):
        raise AssertionError("delta sync should not fall back to window sync")


class FakeContactsDeltaAdapter:
    def discover_addressbooks(self, **kwargs):
        return [
            SyncedAddressBook(
                id="addr_remote",
                url="https://contacts.example/addressbook/",
                display_name="Contacts",
                sync_token="token-new",
                ctag="ctag-new",
            )
        ]

    def sync_contact_changes(self, **kwargs):
        addressbook = kwargs["addressbook"]
        return (
            ContactSyncResult(sync_token="token-new", changed=[], deleted=["2.vcf"]),
            [
                SyncedContact(
                    id="contact_1",
                    addressbook_id=addressbook.id,
                    href="https://contacts.example/addressbook/1.vcf",
                    etag='"v2"',
                    uid="contact-1",
                    raw_vcard="BEGIN:VCARD\nFN:Liesa Updated\nEMAIL:liesa@example.com\nEND:VCARD",
                    display_name="Liesa Updated",
                    given_name="Liesa",
                    family_name="Updated",
                    emails=["liesa@example.com"],
                    phones=[],
                    organization=None,
                    notes=None,
                )
            ],
        )

    def sync_contacts(self, **kwargs):
        raise AssertionError("delta sync should not fall back to full contact sync")


class SyncWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(database_path=":memory:", apple_id="user@example.com", app_password="app-pass")
        self.db = open_db(":memory:")
        ensure_defaults(self.db, self.settings)

    def tearDown(self) -> None:
        self.db.close()

    def test_workers_sync_remote_data_into_search(self) -> None:
        ContactsSyncWorker(self.db, self.settings, FakeContactsAdapter()).run_once()
        CalendarSyncWorker(self.db, self.settings, FakeCalendarAdapter()).run_once()
        MailSyncWorker(self.db, self.settings, FakeMailAdapter()).run_once()

        results = search_documents(
            self.db,
            query="Liesa sync contract",
            domains=["contact", "calendar", "mail"],
            limit=10,
            offset=0,
            snippet_chars=300,
        )
        ids = {result["id"] for result in results}

        self.assertIn("contact_1", ids)
        self.assertIn("cal_evt_1", ids)
        self.assertIn("mail_msg_1", ids)

    def test_mail_backfill_syncs_older_mail_batch(self) -> None:
        MailSyncWorker(self.db, self.settings, FakeMailAdapter()).run_once()

        result = MailBackfillWorker(self.db, self.settings, FakeMailBackfillAdapter()).run_once()
        MailSyncWorker(self.db, self.settings, FakeMailAdapter()).run_once()
        older = search_documents(
            self.db,
            query="archived contract",
            domains=["mail"],
            limit=10,
            offset=0,
            snippet_chars=300,
        )

        self.assertEqual(result["backfill_status"], "complete")
        self.assertEqual(older[0]["id"], "mail_msg_older")
        self.assertEqual(sync_status(self.db)["workers"]["mail_backfill_worker"]["status"], "ok")
        self.assertEqual(
            self.db.query_one("SELECT backfill_status FROM mailboxes WHERE id = ?", ("mb_inbox",))["backfill_status"],
            "complete",
        )

    def test_mail_incremental_sync_tombstones_deleted_uids(self) -> None:
        MailSyncWorker(self.db, self.settings, FakeMailAdapter()).run_once()

        adapter = FakeMailIncrementalAdapter()
        MailSyncWorker(self.db, self.settings, adapter).run_once()
        old_message = self.db.query_one("SELECT deleted_at FROM mail_messages WHERE id = ?", ("mail_msg_1",))
        new_results = search_documents(
            self.db,
            query="delta",
            domains=["mail"],
            limit=10,
            offset=0,
            snippet_chars=300,
        )

        self.assertEqual(adapter.mailbox_states["mb_inbox"]["known_uids"], [1])
        self.assertIsNotNone(old_message["deleted_at"])
        self.assertEqual(new_results[0]["id"], "mail_msg_3")

    def test_calendar_sync_uses_sync_token_deletions(self) -> None:
        upsert_calendar_collection(
            self.db,
            account_id=self.settings.default_account_id,
            calendar_id="cal_remote",
            url="https://caldav.example/cal/",
            display_name="Calendar",
            sync_token="token-old",
            ctag="ctag-old",
        )
        upsert_calendar_object(
            self.db,
            calendar_id="cal_remote",
            event_id="cal_evt_deleted",
            href="https://caldav.example/cal/2.ics",
            uid="event-2",
            etag='"v1"',
            raw_ics="BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:event-2\nSUMMARY:Deleted\nEND:VEVENT\nEND:VCALENDAR",
            summary="Deleted",
            description=None,
            location=None,
            dtstart="2026-04-27T10:00:00+00:00",
            dtend="2026-04-27T11:00:00+00:00",
            timezone="UTC",
        )

        CalendarSyncWorker(self.db, self.settings, FakeCalendarDeltaAdapter()).run_once()

        calendar = self.db.query_one("SELECT sync_token FROM calendar_collections WHERE id = ?", ("cal_remote",))
        deleted = self.db.query_one("SELECT deleted_at FROM calendar_objects WHERE id = ?", ("cal_evt_deleted",))
        self.assertEqual(calendar["sync_token"], "token-new")
        self.assertIsNotNone(deleted["deleted_at"])

    def test_contacts_sync_uses_sync_token_deletions(self) -> None:
        upsert_addressbook(
            self.db,
            account_id=self.settings.default_account_id,
            addressbook_id="addr_remote",
            url="https://contacts.example/addressbook/",
            display_name="Contacts",
            sync_token="token-old",
            ctag="ctag-old",
        )
        upsert_contact(
            self.db,
            addressbook_id="addr_remote",
            contact_id="contact_deleted",
            href="https://contacts.example/addressbook/2.vcf",
            raw_vcard="BEGIN:VCARD\nFN:Deleted Contact\nEND:VCARD",
            display_name="Deleted Contact",
            emails=[],
        )

        ContactsSyncWorker(self.db, self.settings, FakeContactsDeltaAdapter()).run_once()

        addressbook = self.db.query_one("SELECT sync_token FROM addressbooks WHERE id = ?", ("addr_remote",))
        deleted = self.db.query_one("SELECT deleted_at FROM contacts WHERE id = ?", ("contact_deleted",))
        updated = search_documents(self.db, query="updated", domains=["contact"], limit=10, offset=0, snippet_chars=300)
        self.assertEqual(addressbook["sync_token"], "token-new")
        self.assertIsNotNone(deleted["deleted_at"])
        self.assertEqual(updated[0]["id"], "contact_1")

    def test_scheduler_marks_embeddings_ready(self) -> None:
        MailSyncWorker(self.db, self.settings, FakeMailAdapter()).run_once()
        scheduler = SyncScheduler(self.db, Settings(database_path=":memory:"))
        result = scheduler.sync_now()
        chunk = self.db.query_one("SELECT embedding_status, embedding_model FROM search_chunks LIMIT 1")

        self.assertIn("embedding_worker", result)
        self.assertEqual(chunk["embedding_status"], "ready")
        self.assertEqual(sync_status(self.db)["workers"]["embedding_worker"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
