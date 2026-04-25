from __future__ import annotations

import unittest
from email import message_from_bytes
from email.header import Header
from email.message import Message

from icalendar import Calendar

from icloud_mcp.calendar.adapter import CalDAVCalendarAdapter, _parse_sync_collection_response, _synced_event
from icloud_mcp.calendar.cache import build_ics
from icloud_mcp.contacts.adapter import (
    _contact_from_vcard,
)
from icloud_mcp.contacts.adapter import (
    _parse_sync_collection_response as _parse_carddav_sync_collection_response,
)
from icloud_mcp.mail.adapter import _message_from_email, _message_id


class ProtocolAdapterParsingTests(unittest.TestCase):
    def test_imap_message_parser_extracts_html_body(self) -> None:
        raw = b"""From: Liesa <liesa@example.com>
To: Me <me@example.com>
Subject: =?utf-8?q?Project_Sync?=
Date: Fri, 24 Apr 2026 09:00:00 +0200
Message-ID: <msg-1@example.com>
MIME-Version: 1.0
Content-Type: text/html; charset=utf-8

<html><body><script>bad()</script><p>Let's meet Monday at 14:00.</p></body></html>
"""
        parsed = _message_from_email(
            mailbox_id="mb_inbox",
            uid=1,
            message=message_from_bytes(raw),
            flags=(b"\\Seen",),
            size_bytes=len(raw),
            internal_date=None,
        )

        self.assertEqual(parsed.subject, "Project Sync")
        self.assertEqual(parsed.from_address["email"], "liesa@example.com")
        self.assertIn("Monday at 14:00", parsed.body_text)
        self.assertNotIn("bad()", parsed.body_text)

    def test_imap_message_parser_extracts_headers_attachments_and_invites(self) -> None:
        raw = b"""From: Liesa <liesa@example.com>
To: Me <me@example.com>
Bcc: Archive <archive@example.com>
Subject: Invite with attachment
Date: Fri, 24 Apr 2026 09:00:00 +0200
Message-ID: <msg-2@example.com>
In-Reply-To: <root@example.com>
References: <root@example.com> <prev@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="b"

--b
Content-Type: text/plain; charset=utf-8

Let's meet Monday.
--b
Content-Type: text/calendar; charset=utf-8

BEGIN:VCALENDAR
METHOD:REQUEST
BEGIN:VEVENT
UID:invite-1
SUMMARY:Project Sync
DTSTART:20260427T120000Z
DTEND:20260427T130000Z
ORGANIZER;CN=Liesa:mailto:liesa@example.com
ATTENDEE;CN=Me:mailto:me@example.com
END:VEVENT
END:VCALENDAR
--b
Content-Disposition: attachment; filename="agenda.txt"
Content-Type: text/plain

agenda
--b--
"""
        parsed = _message_from_email(
            mailbox_id="mb_inbox",
            uid=2,
            message=message_from_bytes(raw),
            flags=(),
            size_bytes=len(raw),
            internal_date=None,
        )

        self.assertEqual(parsed.bcc_addresses[0]["email"], "archive@example.com")
        self.assertEqual(parsed.in_reply_to, "<root@example.com>")
        self.assertEqual(parsed.references, ["<root@example.com>", "<prev@example.com>"])
        self.assertEqual(parsed.attachments[0]["filename"], "agenda.txt")
        self.assertEqual(parsed.calendar_invites[0]["uid"], "invite-1")

    def test_imap_message_parser_handles_non_string_attachment_disposition(self) -> None:
        message = Message()
        message._headers.append(("Content-Type", 'multipart/mixed; boundary="b"'))
        part = Message()
        part._headers.append(("Content-Disposition", Header('attachment; filename="agenda.txt"', "utf-8")))
        part._headers.append(("Content-Type", "text/plain"))
        part.set_payload("agenda")
        message.set_payload([part])

        parsed = _message_from_email(
            mailbox_id="mb_inbox",
            uid=3,
            message=message,
            flags=(),
            size_bytes=0,
            internal_date=None,
        )

        self.assertEqual(parsed.attachments[0]["filename"], "agenda.txt")

    def test_imap_message_parser_skips_encrypted_body_text(self) -> None:
        raw = b"""From: Liesa <liesa@example.com>
To: Me <me@example.com>
Subject: Encrypted
Date: Fri, 24 Apr 2026 09:00:00 +0200
Message-ID: <msg-enc@example.com>
MIME-Version: 1.0
Content-Type: application/pkcs7-mime

encrypted payload that should not be indexed
"""
        parsed = _message_from_email(
            mailbox_id="mb_inbox",
            uid=3,
            message=message_from_bytes(raw),
            flags=(),
            size_bytes=len(raw),
            internal_date=None,
        )

        self.assertEqual(parsed.body_text, "")
        self.assertEqual(parsed.body_unavailable_reason, "encrypted_or_signed")

    def test_imap_stable_id_includes_mailbox_and_uid(self) -> None:
        first = _message_id("mb_inbox", 1, "<shared@example.com>")
        same_uid_changed_header = _message_id("mb_inbox", 1, "<changed@example.com>")
        duplicate_in_archive = _message_id("mb_archive", 1, "<shared@example.com>")
        duplicate_in_mailbox = _message_id("mb_inbox", 2, "<shared@example.com>")

        self.assertEqual(first, same_uid_changed_header)
        self.assertNotEqual(first, duplicate_in_archive)
        self.assertNotEqual(first, duplicate_in_mailbox)

    def test_carddav_vcard_parser_extracts_alias_fields(self) -> None:
        contact = _contact_from_vcard(
            addressbook_id="addr_1",
            href="https://contacts.icloud.com/card/1.vcf",
            etag='"abc"',
            raw_vcard="""BEGIN:VCARD
VERSION:3.0
UID:contact-1
FN:Liesa Müller
N:Müller;Liesa;;;
ORG:Example GmbH
EMAIL:liesa@example.com
TEL:+491234
NOTE:private note
END:VCARD
""",
        )

        self.assertEqual(contact.display_name, "Liesa Müller")
        self.assertEqual(contact.given_name, "Liesa")
        self.assertEqual(contact.family_name, "Müller")
        self.assertEqual(contact.emails, ["liesa@example.com"])

    def test_carddav_vcard_parser_extracts_extra_aliases(self) -> None:
        contact = _contact_from_vcard(
            addressbook_id="addr_1",
            href="https://contacts.icloud.com/card/2.vcf",
            etag='"abc"',
            raw_vcard="""BEGIN:VCARD
VERSION:3.0
UID:contact-2
FN:Elizabeth Example
N:Example;Elizabeth;;;
NICKNAME:Liesa
EMAIL:elizabeth@example.com
RELATED:Project Sponsor
END:VCARD
""",
        )

        aliases = {alias for alias, _, _ in contact.extra_aliases}
        self.assertIn("Liesa", aliases)
        self.assertIn("Project Sponsor", aliases)

    def test_carddav_sync_collection_parser_extracts_changed_and_deleted_hrefs(self) -> None:
        parsed = _parse_carddav_sync_collection_response(
            """<?xml version="1.0" encoding="utf-8" ?>
<d:multistatus xmlns:d="DAV:">
  <d:sync-token>sync-token-2</d:sync-token>
  <d:response>
    <d:href>/carddav/contacts/1.vcf</d:href>
    <d:propstat>
      <d:prop><d:getetag>"etag-2"</d:getetag></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/carddav/contacts/old.vcf</d:href>
    <d:status>HTTP/1.1 404 Not Found</d:status>
  </d:response>
</d:multistatus>
"""
        )

        self.assertEqual(parsed.sync_token, "sync-token-2")
        self.assertEqual(parsed.changed[0].href, "/carddav/contacts/1.vcf")
        self.assertEqual(parsed.changed[0].etag, '"etag-2"')
        self.assertEqual(parsed.deleted, ["/carddav/contacts/old.vcf"])

    def test_caldav_ics_parser_extracts_event_fields(self) -> None:
        event = type("Event", (), {"url": "https://caldav.icloud.com/e/1.ics", "etag": '"v1"'})()
        synced = _synced_event(
            "cal_1",
            event,
            """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:event-1
SUMMARY:Project Sync with Liesa
DTSTART:20260427T120000Z
DTEND:20260427T130000Z
LOCATION:Zoom
ATTENDEE;CN=Liesa:mailto:liesa@example.com
END:VEVENT
END:VCALENDAR
""",
        )

        self.assertEqual(synced.uid, "event-1")
        self.assertEqual(synced.summary, "Project Sync with Liesa")
        self.assertEqual(synced.attendees, [{"email": "liesa@example.com", "name": "Liesa"}])

    def test_caldav_expanded_recurrences_get_distinct_local_hrefs(self) -> None:
        event = type(
            "Event",
            (),
            {"url": "https://caldav.icloud.com/e/recurring.ics", "props": {"{DAV:}getetag": '"v1"'}},
        )()
        first = _synced_event(
            "cal_1",
            event,
            """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:event-1
SUMMARY:Weekly Sync
DTSTART:20260427T120000Z
DTEND:20260427T130000Z
RECURRENCE-ID:20260427T120000Z
END:VEVENT
END:VCALENDAR
""",
        )
        second = _synced_event(
            "cal_1",
            event,
            """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:event-1
SUMMARY:Weekly Sync
DTSTART:20260504T120000Z
DTEND:20260504T130000Z
RECURRENCE-ID:20260504T120000Z
END:VEVENT
END:VCALENDAR
""",
        )

        self.assertNotEqual(first.href, second.href)
        self.assertNotEqual(first.id, second.id)
        self.assertTrue(first.href.startswith("https://caldav.icloud.com/e/recurring.ics#recurrence-"))
        self.assertEqual(first.etag, '"v1"')

    def test_caldav_sync_collection_parser_extracts_changed_and_deleted_hrefs(self) -> None:
        parsed = _parse_sync_collection_response(
            """<?xml version="1.0" encoding="utf-8" ?>
<d:multistatus xmlns:d="DAV:">
  <d:sync-token>sync-token-3</d:sync-token>
  <d:response>
    <d:href>/caldav/calendars/1.ics</d:href>
    <d:propstat>
      <d:prop><d:getetag>"event-etag"</d:getetag></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/caldav/calendars/deleted.ics</d:href>
    <d:status>HTTP/1.1 404 Not Found</d:status>
  </d:response>
</d:multistatus>
"""
        )

        self.assertEqual(parsed.sync_token, "sync-token-3")
        self.assertEqual(parsed.changed[0].href, "/caldav/calendars/1.ics")
        self.assertEqual(parsed.changed[0].etag, '"event-etag"')
        self.assertEqual(parsed.deleted, ["/caldav/calendars/deleted.ics"])

    def test_caldav_sync_changes_preserves_report_etag(self) -> None:
        client = _FakeCalDAVClient()
        client.event.etag = None
        client.event.data = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:event-1
SUMMARY:Changed
DTSTART:20260427T120000Z
DTEND:20260427T130000Z
END:VEVENT
END:VCALENDAR
"""
        adapter = _FakeCalDAVCalendarAdapter(client)

        _, events = adapter.sync_event_changes(
            apple_id="person@example.com",
            app_password="app-password",
            calendar_id="cal_1",
            calendar_url="https://caldav.icloud.com/e/",
            sync_token="sync-token-1",
        )

        self.assertEqual(events[0].etag, '"event-etag"')

    def test_caldav_update_event_sends_if_match_header(self) -> None:
        raw_ics = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:event-1
SUMMARY:Updated
DTSTART:20260427T120000Z
DTEND:20260427T130000Z
END:VEVENT
END:VCALENDAR
"""
        client = _FakeCalDAVClient()
        adapter = _FakeCalDAVCalendarAdapter(client)

        result = adapter.update_event(
            apple_id="person@example.com",
            app_password="app-password",
            event_href="https://caldav.icloud.com/e/1.ics",
            raw_ics=raw_ics,
            expected_etag='"v1"',
        )

        self.assertEqual(result.uid, "event-1")
        self.assertEqual(client.put_headers["If-Match"], '"v1"')
        self.assertEqual(client.put_headers["Content-Type"], 'text/calendar; charset="utf-8"')
        self.assertEqual(client.put_body, raw_ics)

    def test_caldav_update_event_strips_local_recurrence_fragment(self) -> None:
        client = _FakeCalDAVClient()
        adapter = _FakeCalDAVCalendarAdapter(client)

        adapter.update_event(
            apple_id="person@example.com",
            app_password="app-password",
            event_href="https://caldav.icloud.com/e/1.ics#recurrence-local",
            raw_ics="""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:event-1
SUMMARY:Updated
DTSTART:20260427T120000Z
DTEND:20260427T130000Z
END:VEVENT
END:VCALENDAR
""",
            expected_etag='"v1"',
        )

        self.assertEqual(client.event_href, "https://caldav.icloud.com/e/1.ics")

    def test_build_ics_generates_parseable_calendar_data(self) -> None:
        raw_ics = build_ics(
            uid="event-1",
            title="Codex MCP verification",
            start="2026-04-30T09:10:00+02:00",
            end="2026-04-30T09:20:00+02:00",
            timezone="Europe/Berlin",
            location="Berlin",
            description="Verify CalDAV write data",
            attendees=[{"email": "liesa@example.com", "name": "Liesa"}],
            recurrence={"freq": "weekly", "count": 2},
            alarms=[{"minutes_before": 15}],
        )

        calendar = Calendar.from_ical(raw_ics)
        event = next(item for item in calendar.walk() if item.name == "VEVENT")

        self.assertEqual(str(event.get("SUMMARY")), "Codex MCP verification")
        self.assertEqual(str(event.get("UID")), "event-1")
        self.assertEqual(str(event.get("LOCATION")), "Berlin")
        self.assertEqual(str(event.get("ATTENDEE")), "mailto:liesa@example.com")


class _FakeCalDAVCalendarAdapter(CalDAVCalendarAdapter):
    def __init__(self, client: _FakeCalDAVClient) -> None:
        super().__init__()
        self.client = client

    def _client(self, apple_id: str, app_password: str) -> _FakeCalDAVClient:
        return self.client


class _FakeCalDAVClient:
    def __init__(self) -> None:
        self.event = _FakeCalDAVEvent(self)
        self.calendar = _FakeCalDAVCalendar(self)
        self.event_href = ""
        self.put_headers: dict[str, str] = {}
        self.put_body = ""

    def __enter__(self) -> _FakeCalDAVClient:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def principal(self) -> object:
        return type("Principal", (), {"calendars": lambda _: [self.calendar]})()

    def event_by_url(self, event_href: str) -> _FakeCalDAVEvent:
        self.event_href = event_href
        return self.event

    def report(self, url: str, body: str, depth: int) -> object:
        return type(
            "Response",
            (),
            {
                "text": """<d:multistatus xmlns:d="DAV:">
  <d:sync-token>sync-token-2</d:sync-token>
  <d:response>
    <d:href>1.ics</d:href>
    <d:propstat>
      <d:prop><d:getetag>"event-etag"</d:getetag></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>"""
            },
        )()

    def put(self, url: str, body: str, headers: dict[str, str]) -> _FakeCalDAVResponse:
        self.put_body = body
        self.put_headers = headers
        return _FakeCalDAVResponse()


class _FakeCalDAVCalendar:
    def __init__(self, client: _FakeCalDAVClient) -> None:
        self.client = client
        self.url = "https://caldav.icloud.com/e/"


class _FakeCalDAVEvent:
    def __init__(self, client: _FakeCalDAVClient) -> None:
        self.client = client
        self.url = "https://caldav.icloud.com/e/1.ics"
        self.etag = '"v1"'
        self.data = ""


class _FakeCalDAVResponse:
    status = 204


if __name__ == "__main__":
    unittest.main()
