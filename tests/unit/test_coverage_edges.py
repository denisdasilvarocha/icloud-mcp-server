from __future__ import annotations

import asyncio
import importlib
import logging
import runpy
import unittest
from datetime import UTC, date, datetime
from email import message_from_bytes
from threading import RLock
from types import SimpleNamespace
from unittest.mock import patch

from defusedxml import ElementTree

from icloud_mcp.adapters import caldav_calendar as caldav
from icloud_mcp.adapters import carddav_contacts as carddav
from icloud_mcp.adapters import imap_mail
from icloud_mcp.config import Settings
from icloud_mcp.db import repositories as repo
from icloud_mcp.db.connection import open_db
from icloud_mcp.db.repositories import (
    build_ics,
    create_calendar_event,
    ensure_defaults,
    update_calendar_event,
    upsert_addressbook,
    upsert_calendar_collection,
    upsert_contact,
    upsert_mail_message,
    upsert_mailbox,
    view_event,
    view_mail,
)
from icloud_mcp.indexing.chunker import chunk_text
from icloud_mcp.indexing.query_planner import plan_query
from icloud_mcp.observability.metrics import metrics_snapshot, record_metric
from icloud_mcp.schemas.calendar import CreateEventInput, UpdateEventInput
from icloud_mcp.schemas.contacts import ContactSummary
from icloud_mcp.schemas.mail import MailAddress
from icloud_mcp.schemas.search import SearchResultRow
from icloud_mcp.security.redaction import redact_secret, redact_text
from icloud_mcp.security.secrets import ICloudCredentials, load_icloud_credentials, store_icloud_credentials
from icloud_mcp.server import main, register_resources_and_prompts
from icloud_mcp.services.search import SearchService, _external_domains, _refresh_status, answer_hints
from icloud_mcp.sync.calendar_sync import CalendarSyncWorker
from icloud_mcp.sync.contacts_sync import ContactsSyncWorker
from icloud_mcp.sync.mail_sync import MailBackfillWorker, MailSyncWorker
from icloud_mcp.tools.calendar_tools import _write_exception_status, register_calendar_tools
from icloud_mcp.tools.contact_tools import register_contact_tools
from icloud_mcp.tools.mail_tools import register_mail_tools
from icloud_mcp.tools.search_tools import register_search_tools
from icloud_mcp.tools.sync_tools import register_sync_tools
from icloud_mcp.util import cursor_error, decode_cursor, encode_cursor, next_cursor


class CoverageEdgesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(database_path=":memory:", cursor_secret="secret", sync_on_start=False)
        self.db = open_db(":memory:")
        ensure_defaults(self.db, self.settings)

    def tearDown(self) -> None:
        self.db.close()

    def test_small_value_modules_and_util_edges(self) -> None:
        importlib.import_module("icloud_mcp.db.models")
        self.assertEqual(MailAddress("A", "a@example.com").email, "a@example.com")
        self.assertEqual(ContactSummary("c", "Name", ["n@example.com"]).display_name, "Name")
        self.assertEqual(SearchResultRow("id", "mail", "T", "S", 0.5).score, 0.5)
        self.assertEqual(chunk_text("", 10), [])
        self.assertEqual(decode_cursor(None, "secret"), {"offset": 0})
        cursor = encode_cursor({"offset": 5}, "secret")
        self.assertEqual(decode_cursor(cursor, "secret")["offset"], 5)
        expired = encode_cursor({"offset": 1, "expires_at": "2000-01-01T00:00:00+00:00"}, "secret")
        with self.assertRaisesRegex(ValueError, "expired"):
            decode_cursor(expired, "secret")
        tampered = cursor[:-2] + "xx"
        with self.assertRaisesRegex(ValueError, "Invalid cursor signature"):
            decode_cursor(tampered, "secret")
        self.assertEqual(cursor_error(ValueError("Cursor expired"))["reason"], "expired")
        generated = next_cursor(0, 2, 2, "secret", {"domain": "mail"})
        self.assertEqual(decode_cursor(generated, "secret")["domain"], "mail")
        self.assertIsNone(redact_secret(None))
        self.assertEqual(redact_secret("secret"), "***")
        self.assertEqual(redact_text("keep", allow_unredacted=True), "keep")

    def test_config_env_keychain_logging_metrics_and_reexports(self) -> None:
        env = {
            "ICLOUD_MCP_DATABASE_PATH": "~/tmp/test.sqlite3",
            "ICLOUD_MCP_CURSOR_SECRET": "env-secret",
            "ICLOUD_APPLE_ID": "person@example.com",
            "ICLOUD_MCP_QUERY_CACHE_TTL_SECONDS": "10",
            "ICLOUD_MCP_SYNC_ON_START": "false",
            "ICLOUD_MCP_USE_KEYCHAIN": "true",
            "ICLOUD_MCP_ATTACHMENT_TEXT_INDEXING": "true",
            "ICLOUD_MCP_ALLOW_UNREDACTED_DEBUG": "true",
        }
        with patch.dict("os.environ", env, clear=True), patch(
            "icloud_mcp.security.secrets._read_keychain_password", return_value="app-pass"
        ) as read:
            settings = Settings.from_env()
            credentials = load_icloud_credentials(settings)
        self.assertEqual(settings.cursor_secret, "env-secret")
        self.assertEqual(settings.query_cache_ttl_seconds, 300)
        self.assertFalse(settings.sync_on_start)
        self.assertTrue(settings.attachment_text_indexing)
        self.assertEqual(credentials, ICloudCredentials("person@example.com", "app-pass"))
        read.assert_called()
        with patch.dict("os.environ", {"ICLOUD_MCP_SYNC_INTERVAL_SECONDS": "bad"}, clear=True), self.assertRaisesRegex(
            ValueError, "ICLOUD_MCP_SYNC_INTERVAL_SECONDS must be an integer"
        ):
            Settings.from_env()
        with patch.dict("os.environ", {"ICLOUD_MCP_SYNC_ON_START": "maybe"}, clear=True), self.assertRaisesRegex(
            ValueError, "ICLOUD_MCP_SYNC_ON_START must be a boolean"
        ):
            Settings.from_env()
        with patch.dict("os.environ", {"ICLOUD_MCP_SYNC_INTERVAL_SECONDS": "59"}, clear=True), self.assertRaisesRegex(
            ValueError, "ICLOUD_MCP_SYNC_INTERVAL_SECONDS must be at least 60"
        ):
            Settings.from_env()
        with patch.dict("os.environ", {"ICLOUD_MCP_MAIL_SYNC_DAYS": "3651"}, clear=True), self.assertRaisesRegex(
            ValueError, "ICLOUD_MCP_MAIL_SYNC_DAYS must be at most 3650"
        ):
            Settings.from_env()
        self.assertNotEqual(Settings().cursor_secret, Settings().cursor_secret)

        keyring = SimpleNamespace(set_password=lambda *args: None, get_password=lambda *args: "pw")
        with patch.dict("sys.modules", {"keyring": keyring}):
            store_icloud_credentials("person@example.com", "pw")
        with patch.dict("sys.modules", {"keyring": object()}):
            self.assertIsNone(importlib.import_module("icloud_mcp.security.secrets")._read_keychain_password("x"))

        importlib.import_module("icloud_mcp.observability.logging").configure_logging(logging.DEBUG)
        importlib.import_module("icloud_mcp.adapters.dav_xml").parse_xml("<root/>")
        importlib.import_module("icloud_mcp.indexing.fts")
        record_metric(self.db, "x", 1.25, {"a": "b"})
        record_metric(self.db, "y", 1.0)
        record_metric(self.db, "y", 1.0)
        snapshot = metrics_snapshot(self.db, limit=1)
        self.assertEqual(snapshot["totals"], {"x": 1.25, "y": 2.0})
        self.assertEqual(len(snapshot["recent"]), 1)

    def test_query_planner_relative_windows_and_people(self) -> None:
        now = datetime(2026, 4, 24, 12, tzinfo=UTC)
        cases = [
            ("today meeting", "event_listing"),
            ("yesterday events", "event_listing"),
            ("last week calendar", "event_listing"),
            ("next week meeting", "calendar_time_lookup"),
            ("upcoming month appointment", "event_listing"),
            ("who is Liesa", "person_lookup"),
            ("invite from Liesa", "mail_search"),
            ("next thing", "general_search"),
        ]
        for query, intent in cases:
            plan = plan_query(query, now=now)
            self.assertEqual(plan.intent, intent)
        self.assertEqual(plan_query("mail from Liesa Steiner", now=now).people, ["Liesa Steiner"])
        self.assertEqual(plan_query("mail from Liesa about contract", now=now).people, ["Liesa"])
        self.assertEqual(plan_query("mail from Liesa Steiner regarding contract", now=now).people, ["Liesa Steiner"])
        self.assertEqual(plan_query("party invite", now=now).domains, ["calendar", "mail", "mail_invite"])

    def test_imap_helper_and_fake_client_flows(self) -> None:
        adapter = _FakeIMAPAdapter(_FakeIMAPClient())
        self.assertTrue(adapter.configured("a", "b"))
        with patch("imapclient.IMAPClient", return_value=adapter.client):
            mailboxes, messages = adapter.sync_recent(
                apple_id="a", app_password="b", days=1, limit_per_mailbox=1, mailboxes=["INBOX"]
            )
            delta = adapter.sync_incremental(
                apple_id="a",
                app_password="b",
                mailbox_states={
                    imap_mail._mailbox_id("INBOX"): {
                        "last_synced_uid": 1,
                        "uid_validity": "1",
                        "highest_modseq": "2",
                        "known_uids": [1, 4],
                    }
                },
                days=1,
                limit_per_mailbox=2,
            )
            mailbox, older = adapter.sync_backfill(
                apple_id="a", app_password="b", mailbox="INBOX", cursor="uid:3", limit=2
            )
            empty_cache_delta = adapter.sync_incremental(
                apple_id="a",
                app_password="b",
                mailbox_states={
                    imap_mail._mailbox_id("INBOX"): {
                        "last_synced_uid": 3,
                        "uid_validity": "1",
                        "known_uids": [],
                    }
                },
                days=1,
                limit_per_mailbox=2,
            )
            bounded_first_sync = imap_mail._incremental_uids(adapter.client, {}, since=date(2026, 4, 1))
        self.assertEqual(mailboxes[0].name, "INBOX")
        self.assertEqual(messages[0].subject, "Recent")
        self.assertEqual(delta.deleted[0].uid, 4)
        self.assertEqual([message.uid for message in empty_cache_delta.messages], [1, 2])
        self.assertEqual(bounded_first_sync, [1, 2])
        self.assertIn(["SINCE", date(2026, 4, 1)], adapter.client.searches)
        self.assertEqual(mailbox.backfill_status, "complete")
        self.assertGreaterEqual(len(older), 1)
        self.assertEqual(imap_mail._decode_header(""), "")
        self.assertEqual(imap_mail._as_list(None), [])
        self.assertEqual(imap_mail._string_value(b"x"), "x")
        self.assertIsNone(imap_mail._string_value(None))
        self.assertEqual(imap_mail._string_value(3), "3")
        self.assertIsNone(imap_mail._int_value("bad"))

        invalid_date = message_from_bytes(b"Date: nope\nSubject: Bad\n\nbody")
        self.assertEqual(
            imap_mail._message_date(invalid_date, datetime(2026, 1, 1, tzinfo=UTC)),
            "2026-01-01T00:00:00+00:00",
        )
        encrypted = message_from_bytes(b"Content-Type: multipart/encrypted\n\n")
        self.assertEqual(imap_mail._body_unavailable_reason(encrypted), "encrypted")
        self.assertEqual(imap_mail._fetch_messages(adapter.client, "mb", []), [])
        self.assertEqual(imap_mail._fetch_messages(_FakeIMAPClientMissingRaw(), "mb", [1]), [])
        self.assertEqual(imap_mail._select_folder_condstore(_NoCondstoreClient(), "INBOX")[b"UIDNEXT"], 1)
        self.assertGreater(len(imap_mail._backfill_uids(adapter.client, None)), 0)
        self.assertLessEqual(imap_mail._cursor_before_date(None), date.today())
        self.assertLessEqual(imap_mail._cursor_before_date("bad:cursor"), date.today())
        self.assertIsNone(imap_mail._date_value(object()))
        self.assertEqual(imap_mail._date_value(date(2026, 1, 1)), "2026-01-01")
        self.assertTrue(imap_mail._has_attachments(message_from_bytes(b"Content-Disposition: attachment\n\nx")))
        self.assertEqual(imap_mail._message_date(message_from_bytes(b"Subject: No Date\n\nbody"), None)[:4], "2026")

    def test_caldav_fake_client_flows_and_helpers(self) -> None:
        adapter = _FakeCalDAVAdapter(_FakeCalDAVClient())
        self.assertTrue(adapter.configured("a", "b"))
        calendars = adapter.discover(apple_id="a", app_password="b")
        self.assertEqual(calendars[0].display_name, "Work")
        _, events = adapter.sync_events(apple_id="a", app_password="b", start=date(2026, 1, 1), end=date(2026, 1, 2))
        self.assertEqual(events[0].summary, "Event")
        result, changed = adapter.sync_event_changes(
            apple_id="a", app_password="b", calendar_id=calendars[0].id, calendar_url=calendars[0].url, sync_token="t"
        )
        self.assertEqual(result.changed[0].href, "1.ics")
        self.assertEqual(changed[0].uid, "event-1")
        created = adapter.create_event(
            apple_id="a",
            app_password="b",
            calendar_url=calendars[0].url,
            uid="new",
            title="New",
            start="2026-01-01T10:00:00+00:00",
            end="2026-01-01T11:00:00+00:00",
            timezone="UTC",
            location=None,
            description=None,
            attendees=[],
            recurrence=None,
            alarms=[],
        )
        self.assertEqual(created.uid, "new")
        conflict = adapter.update_event(
            apple_id="a", app_password="b", event_href="https://cal.example/1.ics", raw_ics=EVENT_ICS, expected_etag='"old"'
        )
        self.assertEqual(conflict["status"], "conflict")
        self.assertEqual(caldav._as_list(("a", "b")), ["a", "b"])
        self.assertEqual(caldav._as_list(None), [])
        self.assertIsNone(caldav._call_optional(SimpleNamespace(bad=lambda: (_ for _ in ()).throw(RuntimeError())), "bad"))
        self.assertEqual(caldav._call_optional(SimpleNamespace(ok=lambda: ""), "ok"), None)
        self.assertIsNone(caldav._etag(SimpleNamespace(get_etag=lambda: (_ for _ in ()).throw(RuntimeError()))))
        self.assertEqual(caldav._response_text(SimpleNamespace(raw=b"abc")), "abc")
        self.assertEqual(caldav._response_text(SimpleNamespace(raw="abc")), "abc")
        self.assertIn("object", caldav._response_text(object()))
        no_event = "BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR"
        with self.assertRaisesRegex(ValueError, "VEVENT"):
            caldav._parse_ics(no_event)
        saved = SimpleNamespace(url=None, client=None, save=lambda: setattr(saved, "called", True))
        caldav._save_event(saved, EVENT_ICS, None)
        self.assertTrue(saved.called)
        response = SimpleNamespace(status=500, validate_status=lambda: setattr(response, "checked", True))
        caldav._raise_for_write_failure(response)
        self.assertTrue(response.checked)
        with self.assertRaisesRegex(ValueError, "Calendar URL"):
            caldav._calendar_by_url(_FakeCalDAVClient(url="https://other/"), "missing")
        fallback_client = SimpleNamespace(
            principal=lambda: SimpleNamespace(
                calendars=lambda: [
                    SimpleNamespace(event_by_url=lambda href: (_ for _ in ()).throw(RuntimeError())),
                    SimpleNamespace(event_by_url=lambda href: "event"),
                ]
            )
        )
        self.assertEqual(caldav._event_by_url(fallback_client, "href"), "event")
        with self.assertRaisesRegex(ValueError, "Event URL"):
            caldav._event_by_url(SimpleNamespace(principal=lambda: SimpleNamespace(calendars=lambda: [])), "href")
        parsed = caldav._parse_sync_collection_response('<d:multistatus xmlns:d="DAV:"><d:response/></d:multistatus>')
        self.assertEqual(parsed.changed, [])
        response = SimpleNamespace(status=500)
        with self.assertRaisesRegex(ValueError, "CalDAV write failed"):
            caldav._raise_for_write_failure(response)

    def test_carddav_fake_client_flow_and_helpers(self) -> None:
        adapter = carddav.CardDAVContactsAdapter()
        self.assertTrue(adapter.configured("a", "b"))
        fake_client = _FakeCardDAVClient()
        with patch("icloud_mcp.adapters.carddav_contacts.httpx.Client", return_value=fake_client):
            books, contacts = adapter.sync_contacts(apple_id="a", app_password="b")
        self.assertEqual(books[0].display_name, "Contacts")
        self.assertEqual(contacts[0].display_name, "Liesa")
        with patch("icloud_mcp.adapters.carddav_contacts.httpx.Client", return_value=_FakeCardDAVClient()):
            discovered = adapter.discover_addressbooks(apple_id="a", app_password="b")
        self.assertEqual(discovered[0].ctag, "ctag")
        with patch("icloud_mcp.adapters.carddav_contacts.httpx.Client", return_value=_FakeCardDAVClient()):
            result, changed = adapter.sync_contact_changes(
                apple_id="a", app_password="b", addressbook=discovered[0], sync_token="sync"
            )
        self.assertEqual(result.deleted, ["/ab/old.vcf"])
        self.assertEqual(changed[0].emails, ["liesa@example.com"])
        with self.assertRaisesRegex(ValueError, "principal"):
            adapter._principal_url(_EmptyDAVClient())
        with self.assertRaisesRegex(ValueError, "addressbook home"):
            adapter._addressbook_home_url(_EmptyDAVClient(), "https://example/p/")
        self.assertEqual(adapter._contacts_by_hrefs(_FakeCardDAVClient(), discovered[0], []), [])
        self.assertIn("&lt;", carddav._sync_collection_body("<token>"))
        empty_root = ElementTree.fromstring('<d:multistatus xmlns:d="DAV:"><d:response/></d:multistatus>')
        self.assertEqual(carddav._parse_sync_collection_root(empty_root).changed, [])

    def test_registered_read_tools_resources_and_sync_tools(self) -> None:
        upsert_mailbox(self.db, account_id="local", mailbox_id="mb", name="INBOX")
        upsert_mail_message(
            self.db,
            account_id="local",
            mailbox_id="mb",
            message_id="msg",
            uid=1,
            subject="Hello",
            from_address={"email": "a@example.com"},
            to_addresses=[],
            date="2026-01-01T00:00:00+00:00",
            preview="preview",
            body_text="body",
        )
        upsert_addressbook(self.db, account_id="local", addressbook_id="ab", url="https://ab/", display_name="Contacts")
        upsert_contact(
            self.db,
            addressbook_id="ab",
            contact_id="contact",
            href="https://ab/1.vcf",
            raw_vcard="BEGIN:VCARD\nFN:Liesa\nEND:VCARD",
            display_name="Liesa",
            given_name=None,
            family_name=None,
            emails=["liesa@example.com"],
            phones=[],
            organization=None,
            notes="note",
        )
        created = create_calendar_event(
            self.db,
            calendar_id="cal_primary",
            title="Meeting",
            start="2026-01-01T10:00:00+00:00",
            end="2026-01-01T11:00:00+00:00",
            timezone="UTC",
        )
        mcp = _FakeMCP()
        register_search_tools(mcp, self.db, self.settings)
        register_mail_tools(mcp, self.db, self.settings)
        register_contact_tools(mcp, self.db, self.settings)
        register_calendar_tools(mcp, self.db, self.settings)
        register_sync_tools(mcp, self.db, self.settings)
        register_resources_and_prompts(mcp, self.db, self.settings)

        self.assertEqual(asyncio.run(mcp.tools["icloud.search"]("Hello")).structured_content["results"][0]["id"], "msg")
        self.assertEqual(asyncio.run(mcp.tools["icloud.mail.search"]("Hello")).structured_content["results"][0]["id"], "msg")
        self.assertEqual(
            asyncio.run(mcp.tools["icloud.calendar.search_events"]("Meeting")).structured_content["results"][0]["id"],
            created["event_id"],
        )
        self.assertEqual(asyncio.run(mcp.tools["icloud.mail.list"]())["messages"][0]["id"], "msg")
        self.assertEqual(asyncio.run(mcp.tools["icloud.mail.view"]("msg", include=["attachments"]))["attachments"], [])
        self.assertEqual(asyncio.run(mcp.tools["icloud.contacts.list"]())["contacts"][0]["id"], "contact")
        self.assertEqual(asyncio.run(mcp.tools["icloud.contacts.view"]("contact", include_notes=True))["notes"], "note")
        self.assertEqual(asyncio.run(mcp.tools["icloud.contacts.search"]("liesa"))["contacts"][0]["id"], "contact")
        self.assertEqual(asyncio.run(mcp.tools["icloud.calendar.list_calendars"]())["calendars"][0]["id"], "cal_primary")
        self.assertEqual(asyncio.run(mcp.tools["icloud.calendar.list_events"]())["events"][0]["id"], created["event_id"])
        self.assertEqual(asyncio.run(mcp.tools["icloud.calendar.view_event"](created["event_id"]))["id"], created["event_id"])
        self.assertIn("workers", asyncio.run(mcp.tools["icloud.sync.status"]()))
        with patch("icloud_mcp.tools.sync_tools.SyncScheduler") as scheduler:
            scheduler.return_value.sync_now.return_value = {"worker": {"status": "ok"}}
            self.assertIn("results", asyncio.run(mcp.tools["icloud.sync.now"]()))
        self.assertIn("totals", asyncio.run(mcp.tools["icloud.metrics.snapshot"](limit=1000)))
        self.assertEqual(mcp.resources["mail://{message_id}"]("missing")["status"], "not_found")
        self.assertEqual(mcp.resources["calendar://{event_id}"]("missing")["status"], "not_found")
        self.assertEqual(mcp.resources["contact://{contact_id}"]("missing")["status"], "not_found")

    def test_calendar_write_error_status_and_main_runtime_patch(self) -> None:
        self.assertEqual(
            _write_exception_status(RuntimeError("unauthorized for person@example.com"), self.settings)["status"],
            "credential_revoked_or_expired",
        )
        self.assertEqual(_write_exception_status(RuntimeError("offline"), self.settings)["status"], "connectivity_error")
        fake_mcp = SimpleNamespace(run=lambda transport: None)
        with patch("icloud_mcp.server.Settings.from_env", return_value=self.settings), patch(
            "icloud_mcp.server.open_db", return_value=self.db
        ), patch("icloud_mcp.server.SyncScheduler") as scheduler, patch(
            "icloud_mcp.server.create_server", return_value=fake_mcp
        ):
            main()
        scheduler.return_value.start_background.assert_called_once()
        with patch("icloud_mcp.config.Settings.from_env", return_value=self.settings), patch(
            "icloud_mcp.db.connection.open_db", return_value=self.db
        ), patch("icloud_mcp.sync.scheduler.SyncScheduler"), patch("fastmcp.FastMCP.run"):
            runpy.run_module("icloud_mcp.server", run_name="__main__")

    def test_calendar_write_tool_paths(self) -> None:
        mcp = _FakeMCP()
        register_calendar_tools(mcp, self.db, self.settings)
        valid = CreateEventInput(title="Remote", start="2026-01-01T10:00:00+00:00", end="2026-01-01T11:00:00+00:00", timezone="UTC")
        self.assertEqual(asyncio.run(mcp.tools["icloud.calendar.create_event"](valid))["status"], "credential_missing")
        invalid = CreateEventInput(title="x", start="bad", end="bad", timezone="UTC")
        self.assertEqual(asyncio.run(mcp.tools["icloud.calendar.create_event"](invalid))["status"], "invalid")

        remote_calendar = caldav.SyncedCalendar("cal_remote", "https://cal.example/cal/", "Remote", None, False)
        remote_write = caldav.CalendarWrite(
            href="https://cal.example/new.ics",
            etag='"v1"',
            raw_ics=build_ics(
                uid="uid",
                title="Remote",
                start="2026-01-01T10:00:00+00:00",
                end="2026-01-01T11:00:00+00:00",
                timezone="UTC",
                location=None,
                description=None,
                attendees=[],
                recurrence=None,
                alarms=[],
            ),
            uid="uid",
        )
        def create_remote(**kwargs: object) -> caldav.CalendarWrite:
            uid = str(kwargs["uid"])
            return caldav.CalendarWrite(
                href=f"https://cal.example/{uid}.ics",
                etag='"v1"',
                raw_ics=remote_write.raw_ics.replace("UID:uid", f"UID:{uid}"),
                uid=uid,
            )

        fake_adapter = SimpleNamespace(discover=lambda **kwargs: [remote_calendar], create_event=create_remote)
        with patch("icloud_mcp.tools.calendar_tools.load_icloud_credentials", return_value=ICloudCredentials("a", "b")), patch(
            "icloud_mcp.tools.calendar_tools.CalDAVCalendarAdapter",
            return_value=SimpleNamespace(discover=lambda **kwargs: [], create_event=create_remote),
        ):
            self.assertEqual(asyncio.run(mcp.tools["icloud.calendar.create_event"](valid))["status"], "sync_required")
        with patch("icloud_mcp.tools.calendar_tools.load_icloud_credentials", return_value=ICloudCredentials("a", "b")), patch(
            "icloud_mcp.tools.calendar_tools.CalDAVCalendarAdapter", return_value=fake_adapter
        ):
            created = asyncio.run(mcp.tools["icloud.calendar.create_event"](valid))
            duplicate = asyncio.run(
                mcp.tools["icloud.calendar.create_event"](
                    CreateEventInput(
                        title="Remote",
                        start="2026-01-01T10:00:00+00:00",
                        end="2026-01-01T11:00:00+00:00",
                        timezone="UTC",
                        request_id="req-dup",
                    )
                )
            )
            duplicate_again = asyncio.run(
                mcp.tools["icloud.calendar.create_event"](
                    CreateEventInput(
                        title="Remote",
                        start="2026-01-01T10:00:00+00:00",
                        end="2026-01-01T11:00:00+00:00",
                        timezone="UTC",
                        request_id="req-dup",
                    )
                )
            )
        self.assertEqual(created["status"], "created")
        self.assertEqual(duplicate["event_id"], duplicate_again["event_id"])
        raising_adapter = SimpleNamespace(
            discover=lambda **kwargs: [remote_calendar],
            create_event=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
        )
        with patch("icloud_mcp.tools.calendar_tools.load_icloud_credentials", return_value=ICloudCredentials("a", "b")), patch(
            "icloud_mcp.tools.calendar_tools.CalDAVCalendarAdapter", return_value=raising_adapter
        ):
            self.assertEqual(asyncio.run(mcp.tools["icloud.calendar.create_event"](valid))["status"], "connectivity_error")

        current = repo.get_calendar_object(self.db, created["event_id"])
        self.assertIsNotNone(current)
        remote_update = caldav.CalendarWrite(
            href=current["href"], etag='"v2"', raw_ics=remote_write.raw_ics.replace("Remote", "Updated"), uid="uid"
        )
        update_adapter = SimpleNamespace(update_event=lambda **kwargs: remote_update)
        with patch("icloud_mcp.tools.calendar_tools.load_icloud_credentials", return_value=ICloudCredentials("a", "b")), patch(
            "icloud_mcp.tools.calendar_tools.CalDAVCalendarAdapter", return_value=update_adapter
        ):
            updated = asyncio.run(
                mcp.tools["icloud.calendar.update_event"](
                    UpdateEventInput(event_id=created["event_id"], patch={"title": "Updated"}, etag='"v1"')
                )
            )
        self.assertEqual(updated["status"], "updated")
        self.assertEqual(asyncio.run(mcp.tools["icloud.calendar.update_event"](UpdateEventInput(event_id="", patch={"title": "x"})))["status"], "invalid")
        self.assertEqual(
            asyncio.run(mcp.tools["icloud.calendar.update_event"](UpdateEventInput(event_id=created["event_id"], patch={})))["status"],
            "invalid",
        )
        self.assertEqual(
            asyncio.run(mcp.tools["icloud.calendar.update_event"](UpdateEventInput(event_id="missing", patch={"title": "x"})))[
                "status"
            ],
            "not_found",
        )
        local = create_calendar_event(
            self.db,
            calendar_id="cal_primary",
            title="Local",
            start="2026-01-02T10:00:00+00:00",
            end="2026-01-02T11:00:00+00:00",
            timezone="UTC",
        )
        self.assertEqual(
            asyncio.run(
                mcp.tools["icloud.calendar.update_event"](
                    UpdateEventInput(event_id=local["event_id"], patch={"title": "x"}, etag=local["etag"])
                )
            )["status"],
            "credential_missing",
        )
        with patch("icloud_mcp.tools.calendar_tools.load_icloud_credentials", return_value=ICloudCredentials("a", "b")):
            self.assertEqual(
                asyncio.run(
                    mcp.tools["icloud.calendar.update_event"](
                        UpdateEventInput(event_id=local["event_id"], patch={"title": "x"}, etag=local["etag"])
                    )
                )["status"],
                "sync_required",
            )
        upsert_calendar_collection(
            self.db,
            account_id="local",
            calendar_id="cal_remote_no_etag",
            url="https://cal.example/remote/",
            display_name="Remote",
            read_only=False,
        )
        no_etag = create_calendar_event(
            self.db,
            calendar_id="cal_remote_no_etag",
            title="No ETag",
            start="2026-01-03T10:00:00+00:00",
            end="2026-01-03T11:00:00+00:00",
            timezone="UTC",
            href="https://cal.example/no-etag.ics",
            etag=None,
        )
        self.db.execute("UPDATE calendar_objects SET etag = NULL WHERE id = ?", (no_etag["event_id"],))
        with patch("icloud_mcp.tools.calendar_tools.load_icloud_credentials", return_value=ICloudCredentials("a", "b")):
            self.assertEqual(
                asyncio.run(
                    mcp.tools["icloud.calendar.update_event"](
                        UpdateEventInput(event_id=no_etag["event_id"], patch={"title": "x"})
                    )
                )["status"],
                "conflict",
            )
        from icloud_mcp.tools import calendar_tools as calendar_tool_module

        self.assertEqual(
            calendar_tool_module._calendar_for_write(self.db, self.settings, fake_adapter, "cal_remote_no_etag")["id"],
            "cal_remote_no_etag",
        )
        new_remote = caldav.SyncedCalendar("cal_discovered", "https://cal.example/discovered/", "Discovered", None, False)
        discover_adapter = SimpleNamespace(discover=lambda **kwargs: [new_remote])
        with patch("icloud_mcp.tools.calendar_tools.load_icloud_credentials", return_value=ICloudCredentials("a", "b")):
            self.assertEqual(
                calendar_tool_module._calendar_for_write(self.db, self.settings, discover_adapter, "cal_discovered")["id"],
                "cal_discovered",
            )
        with patch("icloud_mcp.tools.calendar_tools.load_icloud_credentials", return_value=None):
            self.assertIsNone(calendar_tool_module._calendar_for_write(self.db, self.settings, fake_adapter, "missing"))
        remote_dict_adapter = SimpleNamespace(update_event=lambda **kwargs: {"status": "conflict"})
        with patch("icloud_mcp.tools.calendar_tools.load_icloud_credentials", return_value=ICloudCredentials("a", "b")), patch(
            "icloud_mcp.tools.calendar_tools.CalDAVCalendarAdapter", return_value=remote_dict_adapter
        ):
            self.assertEqual(
                asyncio.run(
                    mcp.tools["icloud.calendar.update_event"](
                        UpdateEventInput(event_id=created["event_id"], patch={"title": "x"}, etag='"v2"')
                    )
                )["status"],
                "conflict",
            )
        remote_raising_adapter = SimpleNamespace(update_event=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
        with patch("icloud_mcp.tools.calendar_tools.load_icloud_credentials", return_value=ICloudCredentials("a", "b")), patch(
            "icloud_mcp.tools.calendar_tools.CalDAVCalendarAdapter", return_value=remote_raising_adapter
        ):
            self.assertEqual(
                asyncio.run(
                    mcp.tools["icloud.calendar.update_event"](
                        UpdateEventInput(event_id=created["event_id"], patch={"title": "x"}, etag='"v2"')
                    )
                )["status"],
                "connectivity_error",
            )

    def test_repository_private_edges_and_search_service(self) -> None:
        upsert_mailbox(self.db, account_id="local", mailbox_id="mb_news", name="Newsletters")
        upsert_mail_message(
            self.db,
            account_id="local",
            mailbox_id="mb_news",
            message_id="mail_news",
            uid=1,
            subject="Newsletter",
            from_address={"email": "news@example.com"},
            to_addresses=[],
            date="2026-01-01T00:00:00+00:00",
            preview="preview",
            body_text="Body\n\n> quote\nFrom: someone",
        )
        rows = repo.search_documents(self.db, query="", domains=["mail"], limit=5, offset=0, snippet_chars=80)
        self.assertTrue(rows)
        self.assertEqual(repo.person_alias_terms(self.db, None), [])
        self.assertEqual(repo._weighted_score({"domain": "calendar"}, {"time": {"start": "2999-01-01T00:00:00+00:00"}}, 0), 1.0)
        self.assertFalse(repo._is_upcoming({"date": "bad"}))
        self.assertEqual(repo._mail_backfill_status({"total": 2, "complete": 1}), "partial")
        self.assertFalse(repo._matches_person_filter({"domain": "mail"}, {}, "nobody"))
        self.assertEqual(repo.get_calendar_collection(self.db, "missing"), None)
        self.assertEqual(repo.first_writable_calendar(self.db), None)
        fake_rows = [
            {
                "id": "doc1",
                "domain": "mail",
                "object_id": "obj1",
                "occurrence_id": None,
                "title": "One",
                "canonical_text": "text",
                "metadata_json": "{}",
            },
            {
                "id": "doc1",
                "domain": "mail",
                "object_id": "obj1",
                "occurrence_id": None,
                "title": "One",
                "canonical_text": "text",
                "metadata_json": "{}",
            },
        ]
        with patch("icloud_mcp.db.repositories._add_semantic_results", return_value=fake_rows), patch(
            "icloud_mcp.db.repositories._rerank_rows", side_effect=lambda value: value
        ):
            self.assertEqual(
                repo.search_documents(self.db, query="", domains=["mail"], limit=2, offset=0, snippet_chars=20)[0][
                    "score"
                ],
                1.0,
            )
        with patch("icloud_mcp.db.repositories._add_semantic_results", return_value=[fake_rows[0]]), patch(
            "icloud_mcp.db.repositories._rerank_rows", side_effect=lambda value: value
        ):
            self.assertEqual(len(repo.search_documents(self.db, query="", domains=["mail"], limit=1, offset=0, snippet_chars=20)), 1)
        with patch("icloud_mcp.db.repositories.query_similar_chunks", return_value=[]):
            self.assertEqual(
                repo._add_semantic_results(_SemanticFallbackDb(), query="meeting", domains=["mail"], rows=[], limit=2)[
                    0
                ]["why"],
                ["semantic_match"],
            )
        full_rows = [fake_rows[0]]
        no_query_db = SimpleNamespace(query=lambda *args, **kwargs: self.fail("fallback query should be skipped"))
        self.assertIs(
            repo._add_semantic_results(no_query_db, query="meeting", domains=["mail"], rows=full_rows, limit=1),
            full_rows,
        )
        with patch(
            "icloud_mcp.db.repositories.query_similar_chunks", return_value=[{"chunk_id": "chunk", "distance": 0.2}]
        ):
            self.assertEqual(
                repo._sqlite_vec_semantic_results(
                    _SqliteSemanticDb(), query="sqlite", domains=["mail"], existing={"skip"}, limit=1
                )[0]["score"],
                0.8,
            )
        with patch(
            "icloud_mcp.db.repositories.query_similar_chunks", return_value=[{"chunk_id": "chunk", "distance": 0.01}]
        ):
            self.assertEqual(
                repo._sqlite_vec_semantic_results(
                    _SqliteSemanticDb(), query="nonsense", domains=["mail"], existing={"skip"}, limit=1
                ),
                [],
            )
        alias_db = SimpleNamespace(query=lambda sql, params=(): [{"alias": "Liesa"}, {"alias": "Liesa S"}])
        self.assertEqual(repo.person_alias_terms(alias_db, "Liesa"), ["Liesa", "Liesa S"])
        self.assertFalse(repo._is_upcoming({"time": {}}))
        self.assertTrue(repo._is_upcoming({"date": "2999-01-01T00:00:00"}))
        self.assertTrue(repo._matches_time_filter({"domain": "contact"}, {}, start="2026-01-01", end=None))
        created = create_calendar_event(
            self.db,
            calendar_id="cal_primary",
            title="Series",
            start="2026-01-01T10:00:00+00:00",
            end="2026-01-01T11:00:00+00:00",
            timezone="UTC",
            recurrence={"freq": "daily", "count": 2},
        )
        self.assertIn("raw_ics", view_event(self.db, created["event_id"], include_raw_ics=True))
        self.assertEqual(update_calendar_event(self.db, event_id="missing", patch={"title": "x"}, etag=None, scope="series")["status"], "not_found")
        self.assertEqual(update_calendar_event(self.db, event_id=created["event_id"], patch={"title": "x"}, etag=None, scope="bad")["status"], "unsupported_scope")
        single = update_calendar_event(
            self.db,
            event_id=created["event_id"],
            patch={"title": "One", "occurrence_start": "2026-01-01T10:00:00+00:00"},
            etag=None,
            scope="single",
        )
        self.assertEqual(single["scope"], "single")
        self.db.execute(
            "UPDATE calendar_objects SET dtend = ? WHERE id = ?",
            ("2026-01-01T09:00:00+00:00", created["event_id"]),
        )
        self.assertEqual(
            update_calendar_event(
                self.db,
                event_id=created["event_id"],
                patch={"title": "Future", "occurrence_start": "2026-01-01T09:00:00+00:00"},
                etag=None,
                scope="future",
            )["status"],
            "updated",
        )
        self.assertEqual(
            repo._calendar_occurrence_windows(
                "2026-01-01T10:00:00+00:00", "2026-01-01T11:00:00+00:00", "UTC", "RRULE:BAD"
            ),
            [("2026-01-01T10:00:00+00:00", "2026-01-01T11:00:00+00:00")],
        )
        self.assertEqual(
            repo._calendar_occurrence_windows(
                "2026-01-01T10:00:00+00:00", "2026-01-01T09:00:00+00:00", "UTC", None
            ),
            [("2026-01-01T10:00:00+00:00", "2026-01-01T09:00:00+00:00")],
        )
        self.assertEqual(repo._rrule_text("FREQ=DAILY"), "FREQ=DAILY")
        self.assertEqual(repo._ics_recurrence_exceptions("bad", "UTC"), (set(), set(), set()))
        cancelled_ics = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:x
DTSTART:20260101T100000Z
DTEND:20260101T110000Z
END:VEVENT
BEGIN:VEVENT
UID:x
RECURRENCE-ID:20260101T100000Z
STATUS:CANCELLED
DTSTART:20260101T100000Z
DTEND:20260101T110000Z
END:VEVENT
END:VCALENDAR
"""
        self.assertTrue(repo._ics_recurrence_exceptions(cancelled_ics, "UTC")[2])
        rdate_ics = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:x
DTSTART:20260101T100000Z
DTEND:20260101T110000Z
RDATE:20260102T100000Z
END:VEVENT
END:VCALENDAR
"""
        self.assertEqual(len(repo._non_recurring_windows("2026-01-01T10:00:00+00:00", "2026-01-01T11:00:00+00:00", rdate_ics)), 2)
        exdate_ics = rdate_ics.replace("RDATE:20260102T100000Z", "EXDATE:20260101T100000Z")
        self.assertEqual(repo._non_recurring_windows("2026-01-01T10:00:00+00:00", "2026-01-01T11:00:00+00:00", exdate_ics), [])
        self.assertEqual(repo._stored_rrule_to_recurrence("RRULE:FREQ=DAILY;COUNT=2"), {"freq": "DAILY", "count": 2})
        self.assertEqual(repo._stored_rrule_to_recurrence("RRULE:BAD;COUNT=2"), {"count": 2})
        self.assertEqual(repo._as_ical_list(["x"]), ["x"])
        self.assertEqual(repo._date_list_values(SimpleNamespace(dt=date(2026, 1, 1)), "UTC").pop().hour, 0)
        self.assertEqual(repo._ical_datetime(datetime(2026, 1, 1, 1), "UTC").tzinfo.key, "UTC")
        self.assertEqual(repo._ical_datetime("2026-01-01T01:00:00", "UTC").hour, 1)
        self.assertEqual(repo._occurrence_key(datetime(2026, 1, 1, 1)), "2026-01-01T01:00:00+00:00")
        self.assertEqual(repo._datetime_value("2026-01-01", "UTC").hour, 0)
        current = repo.get_calendar_object(self.db, created["event_id"])
        self.assertIn("BEGIN:VCALENDAR", repo.patch_ics("not ics", {"title": "Fallback"}, current))
        self.assertEqual(repo.patch_ics("BEGIN:VCALENDAR\nBEGIN:VTODO\nEND:VTODO\nEND:VCALENDAR", {"title": "x"}, current), "BEGIN:VCALENDAR\nBEGIN:VTODO\nEND:VTODO\nEND:VCALENDAR")
        self.assertIn(
            "DTSTART",
            repo.patch_ics(
                current["raw_ics"],
                {
                    "start": "2026-01-04",
                    "end": "2026-01-05T12:00:00",
                    "attendees": [{"email": "a@example.com", "name": "A"}],
                    "recurrence": {"freq": "weekly", "count": 1},
                },
                current,
            ),
        )
        attendee_ics = build_ics(
            uid="attendee",
            title="Attendee",
            start="2026-01-01T10:00:00+00:00",
            end="2026-01-01T11:00:00+00:00",
            timezone="UTC",
            location=None,
            description=None,
            attendees=[{"email": "old@example.com", "name": "Old"}],
            recurrence=None,
            alarms=[],
        )
        self.assertIn("new@example.com", repo.patch_ics(attendee_ics, {"attendees": [{"email": "new@example.com"}]}, current))
        self.assertTrue(
            any(
                "title must be" in error
                for error in repo.validate_event_input({"title": "x" * 201, "start": "bad", "end": "bad", "timezone": ""})
            )
        )
        self.assertIn("patch must not be empty", repo.validate_event_patch({}))
        self.assertIn("start is required", repo.validate_event_input({"title": "x", "end": "2026-01-01T00:00:00"}))
        self.assertIn("end is required", repo.validate_event_input({"title": "x", "start": "2026-01-01T00:00:00"}))
        self.assertIn("attendees must be a list", repo.validate_event_input({"title": "x", "start": "2026-01-01T00:00:00", "end": "2026-01-01T01:00:00", "timezone": "UTC", "attendees": "bad"}))
        self.assertTrue(
            any(
                "recurrence count" in error
                for error in repo.validate_event_input(
                    {
                        "title": "x",
                        "start": "2026-01-01T00:00:00",
                        "end": "2026-01-01T01:00:00",
                        "timezone": "UTC",
                        "recurrence": {"count": 731},
                    }
                )
            )
        )
        self.assertIsNone(repo._normalize_phone_alias("abc"))
        self.assertEqual(repo._normalize_phone_alias("+12345678901"), "+12345678901")
        self.assertEqual(repo._normalize_phone_alias("1 (234) 567-8901"), "+12345678901")
        self.assertEqual(repo._normalize_phone_alias("2345678901"), "+12345678901")
        self.assertEqual(repo._normalize_phone_alias("12345"), "12345")
        self.assertIn("Org", repo._contact_aliases("Name", ["n@example.com"], "Given", "Family", "Org"))
        self.assertEqual(repo._mailbox_quality("Promotions"), "newsletter")
        self.assertEqual(repo._searchable_mail_body("\n> quoted\nFrom: x"), "\n> quoted\nFrom: x")
        self.assertEqual(repo._searchable_mail_body("Hi\nOn Tue wrote:\nold"), "Hi")
        self.assertIsNone(view_mail(self.db, "missing", include=[], max_body_chars=10))
        self.assertIn("headers", view_mail(self.db, "mail_news", include=["headers"], max_body_chars=10))
        self.assertEqual(
            repo.list_mail(
                self.db,
                mailbox="Newsletters",
                after="2025-01-01T00:00:00+00:00",
                before="2027-01-01T00:00:00+00:00",
                sender="news@example.com",
                limit=10,
                offset=0,
                cursor_secret="secret",
            )["messages"][0]["id"],
            "mail_news",
        )
        upsert_addressbook(self.db, account_id="local", addressbook_id="ab2", url="https://ab2/", display_name="AB2")
        upsert_contact(
            self.db,
            addressbook_id="ab2",
            contact_id="contact_service",
            href="https://ab2/1.vcf",
            raw_vcard="BEGIN:VCARD\nFN:Liesa\nEMAIL:liesa@example.com\nEND:VCARD",
            display_name="Liesa",
            emails=["liesa@example.com"],
        )
        self.assertEqual(
            repo.list_contacts(self.db, addressbook_id="ab2", limit=1, offset=0, cursor_secret="secret")["contacts"][0][
                "id"
            ],
            "contact_service",
        )
        self.assertIsNone(repo.index_calendar_event(self.db, "missing"))

        service = SearchService(self.db, self.settings)
        first = service.search(
            query="Newsletter",
            domains=["mail"],
            start=None,
            end=None,
            person=None,
            limit=5,
            include_body_snippets=True,
            freshness_policy="allow_stale",
            cursor_payload={"offset": 0},
        )
        second = service.search(
            query="Newsletter",
            domains=["mail"],
            start=None,
            end=None,
            person=None,
            limit=5,
            include_body_snippets=True,
            freshness_policy="allow_stale",
            cursor_payload={"offset": 0},
        )
        self.assertEqual(first["meta"]["cache"], "miss")
        self.assertEqual(second["meta"]["cache"], "hit")
        self.assertEqual(
            service.search(
                query="Liesa",
                domains=["contacts"],
                start=None,
                end=None,
                person="Liesa",
                limit=5,
                include_body_snippets=True,
                freshness_policy="refresh_if_stale",
                cursor_payload={"offset": 0},
            )["meta"]["cache"],
            "miss",
        )
        self.assertEqual(answer_hints("x", []), [])
        self.assertEqual(answer_hints("when meeting", [{"id": "e", "domain": "calendar", "title": "Meet", "time": {"start": "s", "end": "e", "timezone": "UTC"}, "score": 0.9}], "calendar_time_lookup")[0]["type"], "calendar_time")
        self.assertEqual(answer_hints("x", [{"id": "c", "domain": "contacts", "score": 0.8}])[0]["type"], "contact_identity")
        self.assertEqual(answer_hints("x", [{"id": "m", "domain": "mail", "score": 0.8}])[0]["type"], "mail_evidence")
        self.assertEqual(answer_hints("x", [{"id": "a", "domain": "x", "score": 0.8}, {"id": "b", "domain": "x", "score": 0.78}])[0]["type"], "ambiguous_candidates")
        self.assertEqual(answer_hints("x", [{"id": "a", "domain": "x", "score": 0.8}, {"id": "b", "domain": "x", "score": 0.1}]), [])
        self.assertEqual(_external_domains(["contact", "mail"]), ["contacts", "mail"])
        paged_rows = [
            {"id": "one", "document_id": "doc_one", "domain": "mail", "title": "One", "snippet": "one", "score": 1.0},
            {"id": "two", "document_id": "doc_two", "domain": "mail", "title": "Two", "snippet": "two", "score": 0.9},
        ]
        with patch("icloud_mcp.services.search.search_documents", return_value=paged_rows):
            paged = SearchService(self.db, self.settings).search(
                query="paged",
                domains=["mail"],
                start=None,
                end=None,
                person=None,
                limit=1,
                include_body_snippets=True,
                freshness_policy="refresh_if_stale",
                cursor_payload={"offset": 0},
            )
        self.assertEqual(len(paged["results"]), 1)
        self.assertIsNotNone(paged["next_cursor"])
        self.assertEqual(_refresh_status("refresh_if_stale", {"mail": {"status": "fresh"}})["status"], "fresh")
        self.assertEqual(_refresh_status("refresh_if_stale", {"mail": {"status": "never_synced"}})["status"], "refresh_unavailable_inline")

    def test_search_cache_hit_resigns_cursor_with_current_secret(self) -> None:
        for index in range(2):
            repo.upsert_search_document(
                self.db,
                document_id=f"doc_cached_cursor_{index}",
                domain="mail",
                object_id=f"mail_cached_cursor_{index}",
                title=f"Cached cursor {index}",
                text="cached cursor pagination",
                metadata={"date": f"2026-04-24T0{index}:00:00+00:00"},
            )

        first = SearchService(
            self.db,
            Settings(database_path=":memory:", cursor_secret="old-secret", sync_on_start=False),
        ).search(
            query="cached cursor pagination",
            domains=["mail"],
            start=None,
            end=None,
            person=None,
            limit=1,
            include_body_snippets=True,
            freshness_policy="allow_stale",
            cursor_payload={"offset": 0},
        )
        self.assertEqual(decode_cursor(first["next_cursor"], "old-secret")["offset"], 1)

        second = SearchService(
            self.db,
            Settings(database_path=":memory:", cursor_secret="new-secret", sync_on_start=False),
        ).search(
            query="cached cursor pagination",
            domains=["mail"],
            start=None,
            end=None,
            person=None,
            limit=1,
            include_body_snippets=True,
            freshness_policy="allow_stale",
            cursor_payload={"offset": 0},
        )

        self.assertEqual(second["meta"]["cache"], "hit")
        self.assertEqual(decode_cursor(second["next_cursor"], "new-secret")["offset"], 1)

    def test_vector_backend_edges_and_audit(self) -> None:
        vector = importlib.import_module("icloud_mcp.indexing.vector")
        backend = importlib.import_module("icloud_mcp.indexing.vector_backend")
        self.assertEqual(vector.cosine_score("", "doc"), 0.0)
        self.assertGreater(vector.cosine_score("meeting", "appointment"), 0.0)
        with patch("icloud_mcp.indexing.vector.Counter", side_effect=[{"x": 0}, {"x": 1}]):
            self.assertEqual(vector.cosine_score("query", "document"), 0.0)
        self.assertEqual(vector.dense_embedding(""), [0.0] * vector.VECTOR_DIMENSIONS)
        self.assertEqual(vector.cosine_score_vectors({}, {"x": 1}), 0.0)
        self.assertEqual(vector.cosine_score_vectors({"x": 0}, {"x": 0}), 0.0)
        self.assertEqual(vector.cosine_score_vectors({"x": 0}, {"x": 1}), 0.0)
        self.assertEqual(vector.cosine_score_vectors({"x": 1}, {"x": 0}), 0.0)
        fake = _FakeVectorDb()
        with patch("icloud_mcp.indexing.vector_backend.sqlite_vec.load", side_effect=RuntimeError()):
            self.assertFalse(backend.ensure_vector_backend(fake))
        with patch("icloud_mcp.indexing.vector_backend.ensure_vector_backend", return_value=False):
            self.assertFalse(backend.upsert_chunk_vector(fake, "chunk", "text"))
            backend.delete_document_vectors(fake, "doc")
            self.assertEqual(backend.query_similar_chunks(fake, "query", 2), [])
        with patch("icloud_mcp.indexing.vector_backend.ensure_vector_backend", return_value=True), patch(
            "icloud_mcp.indexing.vector_backend.sqlite_vec.serialize_float32", return_value=b"vec"
        ):
            self.assertTrue(backend.upsert_chunk_vector(fake, "chunk", "text"))
            backend.delete_document_vectors(fake, "doc")
            self.assertEqual(backend.query_similar_chunks(fake, "query", 2), [{"chunk_id": "chunk", "distance": 0.1}])
        importlib.import_module("icloud_mcp.observability.audit").audit_calendar_write(self.db, "event", "obj", "ok")

    def test_adapter_direct_remaining_edges(self) -> None:
        with patch("caldav.DAVClient", return_value="client"):
            self.assertEqual(caldav.CalDAVCalendarAdapter()._client("a", "b"), "client")
        parsed = caldav._parse_ics(
            """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:date-event
SUMMARY:Date Event
DTSTART;VALUE=DATE:20260101
DTEND;VALUE=DATE:20260102
ORGANIZER;CN=Owner:mailto:owner@example.com
END:VEVENT
END:VCALENDAR
"""
        )
        self.assertEqual(parsed["organizer"]["email"], "owner@example.com")
        self.assertEqual(parsed["timezone"], "UTC")
        self.assertEqual(caldav._date_value(object())[:4], "2026")
        self.assertEqual(caldav._event_data(SimpleNamespace(data=b"abc")), "abc")
        self.assertIsNone(caldav._event_data(SimpleNamespace(data=object())))
        saved = SimpleNamespace(
            url="https://cal.example/no-client.ics",
            client=SimpleNamespace(),
            save=lambda: setattr(saved, "called", True),
        )
        caldav._save_event(saved, EVENT_ICS, None)
        self.assertTrue(saved.called)
        class SaveOnly:
            url = None
            client = None

            def save(self) -> None:
                self.called = True

        save_only = SaveOnly()
        caldav._save_event(save_only, EVENT_ICS, '"etag"')
        self.assertTrue(save_only.called)
        root = ElementTree.fromstring(
            '<d:multistatus xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
            "<d:response><d:propstat><d:prop><d:resourcetype><card:addressbook/></d:resourcetype>"
            "</d:prop></d:propstat></d:response></d:multistatus>"
        )
        with patch("icloud_mcp.adapters.carddav_contacts._propfind", return_value=root):
            self.assertEqual(carddav.CardDAVContactsAdapter()._addressbooks(_FakeCardDAVClient(), "https://x/"), [])

        adapter = _FakeIMAPAdapter(_FakeIMAPClient(uid_validity=b"changed"))
        with patch("imapclient.IMAPClient", return_value=adapter.client):
            delta = adapter.sync_incremental(
                apple_id="a",
                app_password="b",
                mailbox_states={imap_mail._mailbox_id("INBOX"): {"uid_validity": "old", "known_uids": [1, 2]}},
                days=1,
                limit_per_mailbox=0,
            )
        self.assertEqual([deleted.uid for deleted in delta.deleted], [1, 2])
        html_only = message_from_bytes(b"Content-Type: text/html\n\n<p>Hello</p>")
        self.assertEqual(imap_mail._body_text(html_only), "Hello")
        other = message_from_bytes(b"Content-Type: application/octet-stream\n\nx")
        plain: list[str] = []
        html: list[str] = []
        imap_mail._append_part_text(other, plain, html)
        self.assertEqual((plain, html), ([], []))
        no_payload_part = SimpleNamespace(
            get_content_type=lambda: "text/plain",
            get_payload=lambda decode=False: None if decode else "raw",
        )
        imap_mail._append_part_text(no_payload_part, plain, html)
        self.assertEqual(plain, ["raw"])
        bad_invite = message_from_bytes(b"Content-Type: text/calendar\n\nnot ics")
        self.assertEqual(imap_mail._calendar_invites(bad_invite), [])
        with patch("icloud_mcp.adapters.imap_mail._part_text", return_value=""):
            self.assertEqual(imap_mail._calendar_invites(message_from_bytes(b"Content-Type: text/calendar\n\n")), [])
        no_payload = SimpleNamespace(get_payload=lambda decode=False: None if decode else "raw", get_content_charset=lambda: None)
        self.assertEqual(imap_mail._part_text(no_payload), "raw")

    def test_sync_worker_failure_and_fallback_edges(self) -> None:
        settings = Settings(
            database_path=":memory:",
            cursor_secret="secret",
            apple_id="a",
            app_password="b",
            sync_on_start=False,
        )
        self.assertEqual(CalendarSyncWorker(self.db, settings, adapter=_FailingAdapter()).run_once()["status"], "error")
        self.assertEqual(ContactsSyncWorker(self.db, settings, adapter=_FailingAdapter()).run_once()["status"], "error")
        self.assertEqual(MailSyncWorker(self.db, settings, adapter=_FailingAdapter()).run_once()["status"], "error")
        self.assertEqual(
            MailBackfillWorker(self.db, settings, adapter=SimpleNamespace()).run_once()["reason"],
            "adapter_backfill_unsupported",
        )
        self.assertEqual(
            MailBackfillWorker(self.db, settings, adapter=SimpleNamespace(sync_backfill=lambda **kwargs: None)).run_once()[
                "status"
            ],
            "complete",
        )
        upsert_mailbox(
            self.db,
            account_id="local",
            mailbox_id="mb_backfill_fail",
            name="BackfillFail",
        )
        repo.update_mailbox_state(
            self.db,
            mailbox_id="mb_backfill_fail",
            uid_validity="1",
            uid_next=2,
            highest_modseq=None,
            last_synced_uid=2,
            backfill_cursor="uid:2",
            backfill_status="partial",
            last_sync_at=None,
        )
        self.assertEqual(
            MailBackfillWorker(
                self.db,
                settings,
                adapter=SimpleNamespace(sync_backfill=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom"))),
            ).run_once()["status"],
            "error",
        )
        calendar = caldav.SyncedCalendar("cal_delta", "https://cal.example/cal/", "Cal", None, False, "token", "new")
        upsert_calendar_collection(
            self.db,
            account_id="local",
            calendar_id=calendar.id,
            url=calendar.url,
            display_name=calendar.display_name,
            read_only=False,
            sync_token="old",
            ctag="old",
        )
        synced, events, full_ids, deleted = CalendarSyncWorker(self.db, settings)._sync_with_tokens(
            _CalendarFallbackAdapter(calendar), "a", "b", date(2026, 1, 1), date(2026, 1, 2)
        )
        self.assertEqual(full_ids, {calendar.id})
        self.assertEqual(deleted, [])
        self.assertEqual(events[0].calendar_id, calendar.id)
        self.assertEqual(len(synced), 1)
        self.assertEqual(importlib.import_module("icloud_mcp.sync.calendar_sync")._absolute_member_url(calendar.url, "a.ics"), f"{calendar.url}a.ics")
        stale_event = caldav.SyncedCalendarEvent(
            id="stale_event",
            calendar_id=calendar.id,
            href=f"{calendar.url}stale.ics",
            uid="stale",
            etag='"1"',
            raw_ics=EVENT_ICS,
            summary="Stale",
            description=None,
            location=None,
            dtstart="2026-01-01T10:00:00+00:00",
            dtend="2999-01-01T11:00:00+00:00",
            timezone="UTC",
            attendees=[],
            organizer=None,
            rrule=None,
            recurrence_id=None,
            status=None,
        )
        repo.upsert_calendar_object(
            self.db,
            calendar_id=calendar.id,
            event_id=stale_event.id,
            href=stale_event.href,
            uid=stale_event.uid,
            etag=stale_event.etag,
            raw_ics=stale_event.raw_ics,
            summary=stale_event.summary,
            description=None,
            location=None,
            dtstart=stale_event.dtstart,
            dtend=stale_event.dtend,
            timezone="UTC",
        )
        fresh_event = caldav.SyncedCalendarEvent(
            id="fresh_event",
            calendar_id=calendar.id,
            href=f"{calendar.url}fresh.ics",
            uid="fresh",
            etag='"1"',
            raw_ics=EVENT_ICS,
            summary="Fresh",
            description=None,
            location=None,
            dtstart="2026-01-02T10:00:00+00:00",
            dtend="2999-01-02T11:00:00+00:00",
            timezone="UTC",
            attendees=[],
            organizer=None,
            rrule=None,
            recurrence_id=None,
            status=None,
        )
        CalendarSyncWorker(
            self.db, settings, adapter=SimpleNamespace(sync_events=lambda **kwargs: ([calendar], [fresh_event]))
        ).run_once()
        self.assertIsNotNone(self.db.query_one("SELECT deleted_at FROM calendar_objects WHERE id = ?", (stale_event.id,))["deleted_at"])
        new_calendar = caldav.SyncedCalendar("cal_new_sync", "https://cal.example/new/", "New", None, False, "token", "ctag")
        self.assertEqual(
            CalendarSyncWorker(self.db, settings)._sync_with_tokens(
                _CalendarWindowAdapter(new_calendar), "a", "b", date(2026, 1, 1), date(2026, 1, 2)
            )[2],
            {new_calendar.id},
        )

        book = carddav.SyncedAddressBook("ab_delta", "https://contacts.example/ab/", "AB", "token", "new")
        upsert_addressbook(
            self.db,
            account_id="local",
            addressbook_id=book.id,
            url=book.url,
            display_name=book.display_name,
            sync_token="old",
            ctag="old",
        )
        synced_books, contacts, full_books, deleted_hrefs = ContactsSyncWorker(self.db, settings)._sync_with_tokens(
            _ContactsFallbackAdapter(book), "a", "b"
        )
        self.assertEqual(full_books, {book.id})
        self.assertEqual(contacts[0].addressbook_id, book.id)
        self.assertEqual(deleted_hrefs, [])
        self.assertEqual(len(synced_books), 1)
        self.assertEqual(importlib.import_module("icloud_mcp.sync.contacts_sync")._absolute_member_url(book.url, "1.vcf"), f"{book.url}1.vcf")
        upsert_contact(
            self.db,
            addressbook_id=book.id,
            contact_id="stale_contact",
            href=f"{book.url}stale.vcf",
            raw_vcard="BEGIN:VCARD\nFN:Stale\nEND:VCARD",
            display_name="Stale",
            emails=[],
        )
        fresh_contact = carddav.SyncedContact(
            id="fresh_contact",
            addressbook_id=book.id,
            href=f"{book.url}fresh.vcf",
            etag='"1"',
            uid="fresh",
            raw_vcard="BEGIN:VCARD\nFN:Fresh\nEMAIL:fresh@example.com\nEND:VCARD",
            display_name="Fresh",
            given_name=None,
            family_name=None,
            emails=["fresh@example.com"],
            phones=[],
            organization=None,
            notes=None,
        )
        ContactsSyncWorker(
            self.db, settings, adapter=SimpleNamespace(sync_contacts=lambda **kwargs: ([book], [fresh_contact]))
        ).run_once()
        self.assertIsNotNone(self.db.query_one("SELECT deleted_at FROM contacts WHERE id = ?", ("stale_contact",))["deleted_at"])
        new_book = carddav.SyncedAddressBook("ab_new_sync", "https://contacts.example/new/", "New", "token", "ctag")
        self.assertEqual(ContactsSyncWorker(self.db, settings)._sync_with_tokens(_ContactsWindowAdapter(new_book), "a", "b")[2], {new_book.id})

    def test_sync_scheduler_start_and_loop_edges(self) -> None:
        scheduler_mod = importlib.import_module("icloud_mcp.sync.scheduler")
        settings = Settings(database_path=":memory:", cursor_secret="secret", sync_on_start=True, sync_interval_seconds=1)
        scheduler = scheduler_mod.SyncScheduler(self.db, settings)
        scheduler_mod.SyncScheduler(self.db, Settings(database_path=":memory:", cursor_secret="secret", sync_on_start=False)).start_background()
        with patch("icloud_mcp.sync.scheduler.threading.Thread") as thread:
            scheduler.start_background()
        thread.return_value.start.assert_called_once()
        self.assertTrue(scheduler._threads)
        scheduler._threads = [SimpleNamespace(join=lambda timeout: setattr(scheduler, "joined", timeout))]
        scheduler.stop()
        self.assertEqual(scheduler.joined, 2)
        loop_scheduler = scheduler_mod.SyncScheduler(self.db, settings)
        loop_state = {"count": 0}

        def is_set() -> bool:
            loop_state["count"] += 1
            return loop_state["count"] > 1

        loop_scheduler._stop = SimpleNamespace(is_set=is_set, wait=lambda timeout: None)
        with patch.object(loop_scheduler, "sync_now", return_value={"ok": True}) as sync_now:
            loop_scheduler._loop()
        sync_now.assert_called_once()
        failing_loop = scheduler_mod.SyncScheduler(self.db, settings)
        failing_state = {"count": 0}

        def fail_once_is_set() -> bool:
            failing_state["count"] += 1
            return failing_state["count"] > 1

        failing_loop._stop = SimpleNamespace(is_set=fail_once_is_set, wait=lambda timeout: None)
        with patch.object(failing_loop, "sync_now", side_effect=RuntimeError("boom")):
            failing_loop._loop()
        checkpoint = self.db.query_one("SELECT status FROM sync_checkpoints WHERE name = ?", ("maintenance_worker",))
        self.assertEqual(checkpoint["status"], "error")
        exception_scheduler = scheduler_mod.SyncScheduler(self.db, settings)
        with patch.object(exception_scheduler, "sync_now", return_value={"ok": True}), patch.object(
            exception_scheduler._stop, "wait", side_effect=KeyboardInterrupt
        ), self.assertRaises(KeyboardInterrupt):
            exception_scheduler._loop()
        self.assertFalse(scheduler_mod._in_backoff("bad"))


EVENT_ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:event-1
SUMMARY:Event
DTSTART:20260101T100000Z
DTEND:20260101T110000Z
END:VEVENT
END:VCALENDAR
"""


class _FakeMCP:
    def __init__(self) -> None:
        self.tools = {}
        self.resources = {}
        self.prompts = {}

    def tool(self, name: str, annotations: dict) -> object:
        def decorator(func: object) -> object:
            self.tools[name] = func
            return func

        return decorator

    def resource(self, uri: str) -> object:
        def decorator(func: object) -> object:
            self.resources[uri] = func
            return func

        return decorator

    def prompt(self, func: object) -> object:
        self.prompts[func.__name__] = func
        return func


class _FakeIMAPAdapter(imap_mail.IMAPMailAdapter):
    def __init__(self, client: _FakeIMAPClient) -> None:
        super().__init__()
        self.client = client


class _FakeIMAPClient:
    def __init__(self, uid_validity: bytes = b"1") -> None:
        self.uid_validity = uid_validity
        self.searches: list[list[object]] = []

    def __enter__(self) -> _FakeIMAPClient:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def login(self, apple_id: str, app_password: str) -> None:
        return None

    def list_folders(self) -> list[tuple[tuple[bytes], bytes, str]]:
        return [((), b"/", "INBOX")]

    def select_folder(self, folder: str, readonly: bool = True, condstore: bool = False) -> dict:
        return {b"UIDVALIDITY": self.uid_validity, b"UIDNEXT": 4, b"HIGHESTMODSEQ": b"2", b"EXISTS": 2}

    def search(self, criteria: list[object]) -> list[int]:
        self.searches.append(criteria)
        if criteria and criteria[0] == "UID":
            return [2]
        if criteria and criteria[0] == "MODSEQ":
            return [3]
        if criteria and criteria[0] == "BEFORE":
            return [1]
        return [1, 2]

    def fetch(self, uids: list[int], data: list[bytes]) -> dict[int, dict[bytes, object]]:
        return {
            uid: {
                b"BODY[]": (
                    f"From: Sender <s@example.com>\nTo: Me <me@example.com>\nSubject: {'Recent' if uid > 1 else 'Old'}\n"
                    "Date: Fri, 24 Apr 2026 09:00:00 +0000\n\nbody"
                ).encode(),
                b"FLAGS": (b"\\Seen",),
                b"RFC822.SIZE": 42,
                b"INTERNALDATE": datetime(2026, 4, 24, tzinfo=UTC),
            }
            for uid in uids
        }

    def thread(self, algorithm: str, criteria: list[str]) -> list[tuple[int]]:
        return [(2,)]


class _FakeIMAPClientMissingRaw(_FakeIMAPClient):
    def fetch(self, uids: list[int], data: list[bytes]) -> dict[int, dict[bytes, object]]:
        return {uid: {} for uid in uids}


class _NoCondstoreClient:
    def select_folder(self, folder: str, readonly: bool = True, condstore: bool = False) -> dict[bytes, int]:
        if condstore:
            raise TypeError("condstore unsupported")
        return {b"UIDNEXT": 1}


class _FakeCalDAVAdapter(caldav.CalDAVCalendarAdapter):
    def __init__(self, client: _FakeCalDAVClient) -> None:
        super().__init__()
        self.client = client

    def _client(self, apple_id: str, app_password: str) -> _FakeCalDAVClient:
        return self.client


class _FakeCalDAVClient:
    def __init__(self, url: str = "https://cal.example/cal/") -> None:
        self.calendar = _FakeCalendar(url, self)
        self.event = _FakeCalendarEvent(self, "https://cal.example/1.ics", EVENT_ICS, '"v1"')

    def __enter__(self) -> _FakeCalDAVClient:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def principal(self) -> object:
        return SimpleNamespace(calendars=lambda: [self.calendar])

    def event_by_url(self, href: str) -> _FakeCalendarEvent:
        return self.event

    def report(self, url: str, body: str, depth: int) -> object:
        return SimpleNamespace(
            text="""<d:multistatus xmlns:d="DAV:">
<d:sync-token>next</d:sync-token>
<d:response><d:href>1.ics</d:href><d:propstat><d:prop><d:getetag>"v1"</d:getetag></d:prop></d:propstat></d:response>
</d:multistatus>"""
        )

    def put(self, url: str, body: str, headers: dict[str, str]) -> object:
        self.event.etag = '"v2"'
        return SimpleNamespace(status=204)


class _FakeCalendar:
    def __init__(self, url: str, client: _FakeCalDAVClient) -> None:
        self.url = url
        self.name = "Work"
        self.client = client
        self.read_only = False

    def search(self, **kwargs: object) -> list[_FakeCalendarEvent]:
        return [self.client.event, SimpleNamespace(data="")]

    def add_event(self, raw_ics: str) -> _FakeCalendarEvent:
        return _FakeCalendarEvent(self.client, "https://cal.example/new.ics", raw_ics, '"new"')


class _FakeCalendarEvent:
    def __init__(self, client: _FakeCalDAVClient, url: str, data: str, etag: str) -> None:
        self.client = client
        self.url = url
        self.data = data
        self.etag = etag

    def get_etag(self) -> str:
        return self.etag


class _FakeCardDAVClient:
    def __enter__(self) -> _FakeCardDAVClient:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def request(self, method: str, url: str, headers: dict[str, str], content: bytes) -> object:
        if method == "PROPFIND" and "current-user-principal" in content.decode():
            text = '<d:multistatus xmlns:d="DAV:"><d:response><d:propstat><d:prop><d:current-user-principal><d:href>/p/</d:href></d:current-user-principal></d:prop></d:propstat></d:response></d:multistatus>'
        elif method == "PROPFIND" and "addressbook-home-set" in content.decode():
            text = '<d:multistatus xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav"><d:response><d:propstat><d:prop><card:addressbook-home-set><d:href>/ab/</d:href></card:addressbook-home-set></d:prop></d:propstat></d:response></d:multistatus>'
        elif method == "PROPFIND":
            text = '<d:multistatus xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav" xmlns:cs="http://calendarserver.org/ns/"><d:response><d:href>/ab/</d:href><d:propstat><d:prop><d:displayname>Contacts</d:displayname><d:resourcetype><card:addressbook/></d:resourcetype><d:sync-token>sync</d:sync-token><cs:getctag>ctag</cs:getctag></d:prop></d:propstat></d:response><d:response><d:href>/notab/</d:href><d:propstat><d:prop><d:resourcetype/></d:prop></d:propstat></d:response></d:multistatus>'
        elif "sync-collection" in content.decode():
            text = '<d:multistatus xmlns:d="DAV:"><d:sync-token>sync2</d:sync-token><d:response><d:href>/ab/1.vcf</d:href><d:propstat><d:prop><d:getetag>"2"</d:getetag></d:prop></d:propstat></d:response><d:response><d:href>/ab/old.vcf</d:href><d:status>HTTP/1.1 404 Not Found</d:status></d:response></d:multistatus>'
        else:
            text = '<d:multistatus xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav"><d:response><d:href>/ab/1.vcf</d:href><d:propstat><d:prop><d:getetag>"1"</d:getetag><card:address-data>BEGIN:VCARD\nVERSION:3.0\nUID:1\nFN:Liesa\nEMAIL:liesa@example.com\nEND:VCARD</card:address-data></d:prop></d:propstat></d:response><d:response><d:href>/ab/empty.vcf</d:href></d:response></d:multistatus>'
        return SimpleNamespace(text=text, raise_for_status=lambda: None)


class _EmptyDAVClient:
    def request(self, method: str, url: str, headers: dict[str, str], content: bytes) -> object:
        return SimpleNamespace(text='<d:multistatus xmlns:d="DAV:"/>', raise_for_status=lambda: None)


class _FakeVectorDb:
    def __init__(self) -> None:
        self._lock = RLock()
        self.connection = _FakeVectorConnection()


class _FakeVectorConnection:
    def enable_load_extension(self, enabled: bool) -> None:
        self.enabled = enabled

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> _FakeVectorRows:
        self.last_sql = sql
        self.last_parameters = parameters
        return _FakeVectorRows()

    def commit(self) -> None:
        return None


class _FakeVectorRows:
    def fetchall(self) -> list[dict[str, object]]:
        return [{"chunk_id": "chunk", "distance": 0.1}]


class _FailingAdapter:
    def __getattr__(self, name: str) -> object:
        def fail(**kwargs: object) -> object:
            raise RuntimeError("adapter failed")

        return fail


class _CalendarFallbackAdapter:
    def __init__(self, calendar: caldav.SyncedCalendar) -> None:
        self.calendar = calendar

    def discover(self, **kwargs: object) -> list[caldav.SyncedCalendar]:
        return [self.calendar]

    def sync_event_changes(self, **kwargs: object) -> object:
        raise RuntimeError("delta failed")

    def sync_events(self, **kwargs: object) -> tuple[list[caldav.SyncedCalendar], list[caldav.SyncedCalendarEvent]]:
        return [
            self.calendar
        ], [
            caldav.SyncedCalendarEvent(
                id="event",
                calendar_id=self.calendar.id,
                href=f"{self.calendar.url}event.ics",
                uid="uid",
                etag='"1"',
                raw_ics=EVENT_ICS,
                summary="Event",
                description=None,
                location=None,
                dtstart="2026-01-01T10:00:00+00:00",
                dtend="2026-01-01T11:00:00+00:00",
                timezone="UTC",
                attendees=[],
                organizer=None,
                rrule=None,
                recurrence_id=None,
                status=None,
            )
        ]


class _ContactsFallbackAdapter:
    def __init__(self, addressbook: carddav.SyncedAddressBook) -> None:
        self.addressbook = addressbook

    def discover_addressbooks(self, **kwargs: object) -> list[carddav.SyncedAddressBook]:
        return [self.addressbook]

    def sync_contact_changes(self, **kwargs: object) -> object:
        raise RuntimeError("delta failed")

    def sync_contacts(self, **kwargs: object) -> tuple[list[carddav.SyncedAddressBook], list[carddav.SyncedContact]]:
        return [
            self.addressbook
        ], [
            carddav.SyncedContact(
                id="contact_delta",
                addressbook_id=self.addressbook.id,
                href=f"{self.addressbook.url}1.vcf",
                etag='"1"',
                uid="uid",
                raw_vcard="BEGIN:VCARD\nFN:Delta\nEMAIL:delta@example.com\nEND:VCARD",
                display_name="Delta",
                given_name=None,
                family_name=None,
                emails=["delta@example.com"],
                phones=[],
                organization=None,
                notes=None,
            )
        ]


class _CalendarWindowAdapter:
    def __init__(self, calendar: caldav.SyncedCalendar) -> None:
        self.calendar = calendar

    def discover(self, **kwargs: object) -> list[caldav.SyncedCalendar]:
        return [self.calendar]

    def sync_event_changes(self, **kwargs: object) -> tuple[caldav.WebDAVSyncResult, list[caldav.SyncedCalendarEvent]]:
        return caldav.WebDAVSyncResult(sync_token="token", changed=[], deleted=[]), []

    def sync_events(self, **kwargs: object) -> tuple[list[caldav.SyncedCalendar], list[caldav.SyncedCalendarEvent]]:
        return [self.calendar], []


class _ContactsWindowAdapter:
    def __init__(self, addressbook: carddav.SyncedAddressBook) -> None:
        self.addressbook = addressbook

    def discover_addressbooks(self, **kwargs: object) -> list[carddav.SyncedAddressBook]:
        return [self.addressbook]

    def sync_contact_changes(self, **kwargs: object) -> tuple[carddav.WebDAVSyncResult, list[carddav.SyncedContact]]:
        return carddav.WebDAVSyncResult(sync_token="token", changed=[], deleted=[]), []

    def sync_contacts(self, **kwargs: object) -> tuple[list[carddav.SyncedAddressBook], list[carddav.SyncedContact]]:
        return [self.addressbook], []


class _SemanticFallbackDb:
    def query(self, sql: str, parameters: tuple[object, ...] = ()) -> list[dict[str, object]]:
        return [
            {
                "id": "semantic_doc",
                "domain": "mail",
                "object_id": "semantic_obj",
                "occurrence_id": None,
                "title": "Semantic",
                "canonical_text": "meeting appointment",
                "metadata_json": "{}",
                "vector_json": None,
            }
        ]


class _SqliteSemanticDb:
    def query(self, sql: str, parameters: tuple[object, ...] = ()) -> list[dict[str, object]]:
        return [
            {
                "id": "skip",
                "domain": "mail",
                "object_id": "skip",
                "occurrence_id": None,
                "title": "Skip",
                "canonical_text": "skip",
                "metadata_json": "{}",
                "matched_text": "skip",
                "chunk_id": "chunk",
            },
            {
                "id": "sqlite_doc",
                "domain": "mail",
                "object_id": "sqlite_obj",
                "occurrence_id": None,
                "title": "SQLite",
                "canonical_text": "sqlite",
                "metadata_json": "{}",
                "matched_text": "sqlite",
                "chunk_id": "chunk",
            },
        ]


if __name__ == "__main__":
    unittest.main()
