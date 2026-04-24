from __future__ import annotations

import unittest

from icloud_mcp.config import Settings
from icloud_mcp.db.connection import open_db
from icloud_mcp.db.repositories import (
    create_calendar_event,
    ensure_defaults,
    index_generation,
    list_contacts,
    list_events,
    query_cache_get,
    query_cache_set,
    search_contacts,
    search_documents,
    sync_status,
    update_calendar_event,
    upsert_contact,
    upsert_mail_message,
    upsert_mailbox,
    validate_event_input,
)


class LocalMVPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(database_path=":memory:", cursor_secret="test-secret")
        self.db = open_db(":memory:")
        ensure_defaults(self.db, self.settings)

    def tearDown(self) -> None:
        self.db.close()

    def test_calendar_create_is_indexed_and_idempotent(self) -> None:
        payload = {
            "calendar_id": self.settings.default_calendar_id,
            "title": "Project Sync with Liesa",
            "start": "2026-04-27T14:00:00+02:00",
            "end": "2026-04-27T15:00:00+02:00",
            "timezone": "Europe/Berlin",
            "attendees": [{"email": "liesa@example.com", "name": "Liesa"}],
            "request_id": "req-1",
        }

        first = create_calendar_event(self.db, **payload)
        second = create_calendar_event(self.db, **payload)

        self.assertEqual(first["event_id"], second["event_id"])
        results = search_documents(
            self.db,
            query="meeting Liesa",
            domains=["calendar"],
            limit=10,
            offset=0,
            snippet_chars=300,
        )
        self.assertEqual(results[0]["id"], first["event_id"])
        self.assertEqual(results[0]["domain"], "calendar")

    def test_calendar_update_checks_etag_conflicts(self) -> None:
        created = create_calendar_event(
            self.db,
            calendar_id=self.settings.default_calendar_id,
            title="Focus Time",
            start="2026-04-27T10:00:00+02:00",
            end="2026-04-27T11:00:00+02:00",
            timezone="Europe/Berlin",
        )

        conflict = update_calendar_event(
            self.db,
            event_id=created["event_id"],
            patch={"title": "Deep Work"},
            etag="stale",
            scope="series",
        )
        self.assertEqual(conflict["status"], "conflict")

        updated = update_calendar_event(
            self.db,
            event_id=created["event_id"],
            patch={"title": "Deep Work"},
            etag=created["etag"],
            scope="series",
        )
        self.assertEqual(updated["status"], "updated")
        self.assertEqual(updated["etag"], "local-2")

    def test_calendar_list_uses_cursor_pagination(self) -> None:
        for index in range(2):
            create_calendar_event(
                self.db,
                calendar_id=self.settings.default_calendar_id,
                title=f"Event {index}",
                start=f"2026-04-2{index + 1}T10:00:00+02:00",
                end=f"2026-04-2{index + 1}T11:00:00+02:00",
                timezone="Europe/Berlin",
            )

        page = list_events(
            self.db,
            calendar_ids=[self.settings.default_calendar_id],
            start=None,
            end=None,
            limit=1,
            offset=0,
            cursor_secret=self.settings.cursor_secret,
        )

        self.assertEqual(len(page["events"]), 1)
        self.assertIsNotNone(page["next_cursor"])

    def test_calendar_recurring_event_expands_occurrences(self) -> None:
        create_calendar_event(
            self.db,
            calendar_id=self.settings.default_calendar_id,
            title="Weekly Sync",
            start="2026-04-21T10:00:00+02:00",
            end="2026-04-21T11:00:00+02:00",
            timezone="Europe/Berlin",
            recurrence={"freq": "weekly", "count": 3},
        )

        page = list_events(
            self.db,
            calendar_ids=[self.settings.default_calendar_id],
            start="2026-04-01T00:00:00+02:00",
            end="2026-05-31T00:00:00+02:00",
            limit=10,
            offset=0,
            cursor_secret=self.settings.cursor_secret,
        )

        starts = [event["time"]["start"] for event in page["events"]]
        self.assertEqual(
            starts,
            [
                "2026-04-21T10:00:00+02:00",
                "2026-04-28T10:00:00+02:00",
                "2026-05-05T10:00:00+02:00",
            ],
        )

    def test_calendar_validation_rejects_bad_write(self) -> None:
        errors = validate_event_input(
            {
                "title": "",
                "start": "2026-04-27T12:00:00+02:00",
                "end": "2026-04-27T11:00:00+02:00",
                "timezone": "Europe/Berlin",
                "attendees": [{"email": "not-an-email"}],
            }
        )

        self.assertIn("title is required", errors)
        self.assertIn("end must be after start", errors)
        self.assertIn("invalid attendee email: not-an-email", errors)

    def test_mail_and_contact_upserts_feed_search(self) -> None:
        upsert_mailbox(self.db, account_id=self.settings.default_account_id, mailbox_id="mb_inbox", name="INBOX")
        upsert_mail_message(
            self.db,
            account_id=self.settings.default_account_id,
            mailbox_id="mb_inbox",
            message_id="mail_msg_1",
            uid=1,
            subject="Contract deadline",
            from_address={"name": "Liesa", "email": "liesa@example.com"},
            to_addresses=[{"name": "Me", "email": "me@example.com"}],
            date="2026-04-24T09:00:00+02:00",
            preview="Deadline is Friday.",
            body_text="The contract deadline is Friday at noon.",
        )
        upsert_contact(
            self.db,
            addressbook_id=self.settings.default_addressbook_id,
            contact_id="contact_1",
            href="local://contacts/1.vcf",
            raw_vcard="BEGIN:VCARD\nFN:Liesa Müller\nEMAIL:liesa@example.com\nEND:VCARD",
            display_name="Liesa Müller",
            given_name="Liesa",
            family_name="Müller",
            emails=["liesa@example.com"],
        )

        mail = search_documents(
            self.db, query="contract deadline", domains=["mail"], limit=10, offset=0, snippet_chars=300
        )
        contacts = search_documents(self.db, query="Liesa", domains=["contact"], limit=10, offset=0, snippet_chars=300)

        self.assertEqual(mail[0]["id"], "mail_msg_1")
        self.assertEqual(contacts[0]["id"], "contact_1")

    def test_search_filters_by_person_and_time(self) -> None:
        create_calendar_event(
            self.db,
            calendar_id=self.settings.default_calendar_id,
            title="Project Sync with Liesa",
            start="2026-04-27T14:00:00+02:00",
            end="2026-04-27T15:00:00+02:00",
            timezone="Europe/Berlin",
            attendees=[{"email": "liesa@example.com", "name": "Liesa"}],
        )
        create_calendar_event(
            self.db,
            calendar_id=self.settings.default_calendar_id,
            title="Project Sync with Max",
            start="2026-05-27T14:00:00+02:00",
            end="2026-05-27T15:00:00+02:00",
            timezone="Europe/Berlin",
            attendees=[{"email": "max@example.com", "name": "Max"}],
        )

        results = search_documents(
            self.db,
            query="project sync",
            domains=["calendar"],
            limit=10,
            offset=0,
            snippet_chars=300,
            start="2026-04-01T00:00:00+02:00",
            end="2026-04-30T23:59:59+02:00",
            person="Liesa",
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Project Sync with Liesa")

    def test_contact_search_uses_cursor_pagination(self) -> None:
        for index in range(2):
            upsert_contact(
                self.db,
                addressbook_id=self.settings.default_addressbook_id,
                contact_id=f"contact_{index}",
                href=f"local://contacts/{index}.vcf",
                raw_vcard=f"BEGIN:VCARD\nFN:Example {index}\nEMAIL:example{index}@example.com\nEND:VCARD",
                display_name=f"Example {index}",
                emails=[f"example{index}@example.com"],
            )

        result = search_contacts(
            self.db,
            query="example.com",
            limit=1,
            offset=0,
            cursor_secret=self.settings.cursor_secret,
        )

        self.assertEqual(len(result["contacts"]), 1)
        self.assertIsNotNone(result["next_cursor"])

    def test_query_cache_is_bound_to_index_generation(self) -> None:
        generation = index_generation(self.db)
        query_cache_set(self.db, "cache-key", {"results": []}, generation)

        self.assertEqual(query_cache_get(self.db, "cache-key", generation), {"results": []})
        self.assertIsNone(query_cache_get(self.db, "cache-key", generation + 1))

    def test_contact_list_without_addressbook_lists_synced_contacts(self) -> None:
        upsert_contact(
            self.db,
            addressbook_id="addr_remote",
            contact_id="contact_1",
            href="https://contacts.example/addressbook/1.vcf",
            raw_vcard="BEGIN:VCARD\nFN:Liesa Müller\nEMAIL:liesa@example.com\nEND:VCARD",
            display_name="Liesa Müller",
            emails=["liesa@example.com"],
        )

        result = list_contacts(self.db, addressbook_id=None, limit=10, offset=0, cursor_secret="test-secret")

        self.assertEqual(result["contacts"][0]["id"], "contact_1")

    def test_sync_status_reports_generation_and_freshness(self) -> None:
        status = sync_status(self.db)

        self.assertIn("index_generation", status)
        self.assertIn("index_freshness", status)


if __name__ == "__main__":
    unittest.main()
