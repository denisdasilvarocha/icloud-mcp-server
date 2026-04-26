from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from icloud_mcp.calendar import adapter as caldav
from icloud_mcp.calendar.cache import build_ics
from icloud_mcp.calendar.write import CalendarWriteService
from icloud_mcp.platform.config import Settings
from icloud_mcp.platform.secrets import ICloudCredentials
from icloud_mcp.storage.cache_state import ensure_defaults
from icloud_mcp.storage.connection import open_db


class CalendarWriteServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(database_path=":memory:", cursor_secret="calendar-write-secret", sync_on_start=False)
        self.db = open_db(":memory:")
        ensure_defaults(self.db, self.settings)

    def tearDown(self) -> None:
        self.db.close()

    def test_create_event_reuses_idempotent_response_without_second_remote_create(self) -> None:
        remote_calendar = caldav.SyncedCalendar("cal_remote", "https://cal.example/cal/", "Remote", None, False)
        create_calls = []

        def create_remote(**kwargs: object) -> caldav.CalendarWrite:
            create_calls.append(kwargs)
            uid = str(kwargs["uid"])
            return caldav.CalendarWrite(
                href=f"https://cal.example/{uid}.ics",
                etag='"v1"',
                raw_ics=build_ics(
                    uid=uid,
                    title=str(kwargs["title"]),
                    start=str(kwargs["start"]),
                    end=str(kwargs["end"]),
                    timezone=str(kwargs["timezone"]),
                    location=None,
                    description=None,
                    attendees=[],
                    recurrence=None,
                    alarms=[],
                ),
                uid=uid,
            )

        adapter = SimpleNamespace(discover=lambda **kwargs: [remote_calendar], create_event=create_remote)
        service = CalendarWriteService(self.db, self.settings)
        input_data = {
            "title": "Remote",
            "start": "2026-01-01T10:00:00+00:00",
            "end": "2026-01-01T11:00:00+00:00",
            "timezone": "UTC",
            "request_id": "req-create-once",
        }

        with (
            patch("icloud_mcp.calendar.write.load_icloud_credentials", return_value=ICloudCredentials("a", "b")),
            patch("icloud_mcp.calendar.write.CalDAVCalendarAdapter", return_value=adapter),
        ):
            first = service.create_event(input_data)
            second = service.create_event(input_data)

        self.assertEqual(first, second)
        self.assertEqual(first["status"], "created")
        self.assertEqual(len(create_calls), 1)


if __name__ == "__main__":
    unittest.main()
