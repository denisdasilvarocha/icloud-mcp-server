from __future__ import annotations

import unittest
from datetime import date

from icloud_mcp.adapters.caldav_calendar import SyncedCalendar, SyncedCalendarEvent
from icloud_mcp.adapters.carddav_contacts import SyncedAddressBook, SyncedContact
from icloud_mcp.adapters.imap_mail import SyncedMailbox, SyncedMailMessage
from icloud_mcp.config import Settings
from icloud_mcp.db.connection import open_db
from icloud_mcp.db.repositories import ensure_defaults, search_documents, sync_status
from icloud_mcp.sync.calendar_sync import CalendarSyncWorker
from icloud_mcp.sync.contacts_sync import ContactsSyncWorker
from icloud_mcp.sync.mail_sync import MailSyncWorker
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
