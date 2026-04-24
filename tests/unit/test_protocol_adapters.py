from __future__ import annotations

import unittest
from email import message_from_bytes

from icalendar import Calendar

from icloud_mcp.adapters.caldav_calendar import _synced_event
from icloud_mcp.adapters.carddav_contacts import _contact_from_vcard
from icloud_mcp.adapters.imap_mail import _message_from_email
from icloud_mcp.db.repositories import build_ics


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


if __name__ == "__main__":
    unittest.main()
