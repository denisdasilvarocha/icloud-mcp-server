from __future__ import annotations

import asyncio
import json
import os
import unittest
from pathlib import Path
from typing import Any

from icloud_mcp.mcp.server import create_server
from icloud_mcp.platform.config import Settings
from icloud_mcp.storage.cache_state import ensure_defaults, sync_status
from icloud_mcp.storage.connection import Database, open_db
from icloud_mcp.sync.scheduler import SyncScheduler

LIVE_TESTS_ENABLED = os.getenv("ICLOUD_MCP_LIVE_TESTS") == "1"
EXPECTED_RESULT_WORKERS = {
    "contacts_sync_worker",
    "calendar_sync_worker",
    "mail_sync_worker",
    "mail_backfill_worker",
}
EXPECTED_CHECKPOINTS = EXPECTED_RESULT_WORKERS | {"indexer_worker"}
BAD_STATUSES = {"error", "dead_letter", "backoff"}


@unittest.skipUnless(LIVE_TESTS_ENABLED, "set ICLOUD_MCP_LIVE_TESTS=1 to run live iCloud smoke tests")
class LiveSyncSmokeTests(unittest.TestCase):
    db: Database
    settings: Settings

    def setUp(self) -> None:
        if not os.getenv("ICLOUD_APPLE_ID") or not os.getenv("ICLOUD_APP_PASSWORD"):
            self.skipTest("ICLOUD_APPLE_ID and ICLOUD_APP_PASSWORD are required for live smoke tests")
        self.settings = Settings.from_env()
        self.assertNotEqual(Path(self.settings.database_path), Path(":memory:"))
        self.db = open_db(self.settings.database_path)
        ensure_defaults(self.db, self.settings)

    def tearDown(self) -> None:
        self.db.close()

    def test_live_sync_populates_cache_status_metrics_and_mcp_tools(self) -> None:
        scheduler = SyncScheduler(self.db, self.settings)
        first_result = scheduler.sync_now()
        second_result = scheduler.sync_now()

        self._assert_sync_result_healthy(first_result)
        self._assert_sync_result_healthy(second_result)
        self._assert_cache_tables_consistent(second_result)

        status = sync_status(self.db, self.settings.stale_after_seconds)
        self._assert_status_healthy(status)
        self._assert_no_credentials_leaked(status)

        server = create_server(settings=self.settings, db=self.db)
        asyncio.run(self._assert_mcp_tools(server))

    def _assert_sync_result_healthy(self, result: dict[str, dict[str, Any]]) -> None:
        for worker in EXPECTED_RESULT_WORKERS:
            self.assertIn(worker, result)
        for worker, details in result.items():
            status = details.get("status")
            reason = details.get("reason")
            self.assertNotIn(status, BAD_STATUSES, f"{worker} returned {details}")
            self.assertNotEqual(reason, "credentials_missing", f"{worker} returned {details}")

    def _assert_status_healthy(self, status: dict[str, Any]) -> None:
        self.assertIsInstance(status.get("index_generation"), int)
        self.assertIsInstance(status.get("index_freshness"), dict)
        self.assertIsInstance(status.get("freshness_status"), dict)
        workers = status.get("workers")
        self.assertIsInstance(workers, dict)
        for worker in EXPECTED_CHECKPOINTS:
            self.assertIn(worker, workers)
            self.assertIsInstance(workers[worker].get("detail"), dict)
            self.assertNotIn(workers[worker].get("status"), BAD_STATUSES, f"{worker} checkpoint failed")
            self.assertNotEqual(workers[worker].get("detail", {}).get("reason"), "credentials_missing")
        for domain in ["mail", "calendar", "contacts"]:
            self.assertIn(domain, status["freshness_status"])
            self.assertIn(status["freshness_status"][domain].get("status"), {"healthy", "stale", "never_synced"})

    def _assert_cache_tables_consistent(self, result: dict[str, dict[str, Any]]) -> None:
        self.assertGreaterEqual(self._count("accounts"), 1)
        self.assertGreaterEqual(self._count("calendar_collections"), 1)
        self.assertGreaterEqual(self._count("addressbooks"), 1)

        mail = result["mail_sync_worker"]
        if int(mail.get("mailboxes") or 0) > 0:
            self.assertGreater(self._count("mailboxes"), 0)
        if int(mail.get("messages") or 0) > 0:
            self.assertGreater(self._count("mail_messages", "deleted_at IS NULL"), 0)
            self.assertGreater(self._count("search_documents", "domain = 'mail' AND deleted_at IS NULL"), 0)

        calendar = result["calendar_sync_worker"]
        if int(calendar.get("calendars") or 0) > 0:
            self.assertGreater(self._count("calendar_collections", "url NOT LIKE 'local://%'"), 0)
        if int(calendar.get("events") or 0) > 0:
            self.assertGreater(self._count("calendar_objects", "deleted_at IS NULL"), 0)
            self.assertGreater(self._count("calendar_occurrences"), 0)
            self.assertGreater(self._count("search_documents", "domain = 'calendar' AND deleted_at IS NULL"), 0)

        contacts = result["contacts_sync_worker"]
        if int(contacts.get("addressbooks") or 0) > 0:
            self.assertGreater(self._count("addressbooks", "url NOT LIKE 'local://%'"), 0)
        if int(contacts.get("contacts") or 0) > 0:
            self.assertGreater(self._count("contacts", "deleted_at IS NULL"), 0)
            self.assertGreater(self._count("person_aliases"), 0)
            self.assertGreater(self._count("search_documents", "domain = 'contact' AND deleted_at IS NULL"), 0)

    def _assert_no_credentials_leaked(self, payload: dict[str, Any]) -> None:
        serialized = json.dumps(payload, default=str)
        for secret in [self.settings.apple_id, self.settings.app_password]:
            if secret:
                self.assertNotIn(secret, serialized)

    async def _assert_mcp_tools(self, server: object) -> None:
        status = await self._call_tool(server, "icloud.sync.status", {})
        metrics = await self._call_tool(server, "icloud.metrics.snapshot", {"limit": 50})
        search_query = self._first_search_query()
        search = await self._call_tool(server, "icloud.search", {"query": search_query, "limit": 5})
        repeat_search = await self._call_tool(server, "icloud.search", {"query": search_query, "limit": 5})

        self._assert_status_healthy(status)
        self._assert_no_credentials_leaked(status)
        self.assertIsInstance(metrics.get("totals"), dict)
        self.assertIsInstance(metrics.get("recent"), list)
        self.assertIn("sync.duration_ms", metrics["totals"])
        self.assertIn("results", search)
        if self._count("search_documents", "deleted_at IS NULL") > 0:
            self.assertGreater(len(search["results"]), 0)
        self.assertEqual(search.get("meta", {}).get("cache"), "not_used")
        self.assertIn("results", repeat_search)
        self.assertEqual(repeat_search.get("meta", {}).get("cache"), "not_used")

        await self._assert_mail_tools_when_cached(server)
        await self._assert_contact_tools_when_cached(server)
        await self._assert_calendar_tools_when_cached(server)

    async def _assert_mail_tools_when_cached(self, server: object) -> None:
        row = self.db.query_one(
            """
            SELECT m.id, mb.name
            FROM mail_messages m
            JOIN mailboxes mb ON mb.id = m.mailbox_id
            WHERE m.deleted_at IS NULL
            ORDER BY m.date DESC
            LIMIT 1
            """
        )
        if not row:
            return
        mail_list = await self._call_tool(server, "icloud.mail.list", {"mailbox": row["name"], "limit": 5})
        mail_view = await self._call_tool(
            server,
            "icloud.mail.view",
            {"message_id": row["id"], "include": ["headers"], "max_body_chars": 200},
        )
        mail_search = await self._call_tool(
            server, "icloud.mail.search", {"query": self._mail_query(row["id"]), "limit": 5}
        )

        self.assertIsInstance(mail_list.get("messages"), list)
        self.assertEqual(mail_view.get("id"), row["id"])
        self.assertIn("results", mail_search)

    async def _assert_contact_tools_when_cached(self, server: object) -> None:
        row = self.db.query_one(
            """
            SELECT id, display_name, emails_json
            FROM contacts
            WHERE deleted_at IS NULL
            ORDER BY display_name
            LIMIT 1
            """
        )
        if not row:
            return
        contacts_list = await self._call_tool(server, "icloud.contacts.list", {"limit": 5})
        contacts_view = await self._call_tool(server, "icloud.contacts.view", {"contact_id": row["id"]})
        contacts_search = await self._call_tool(
            server,
            "icloud.contacts.search",
            {"query": self._contact_query(row), "limit": 5},
        )

        self.assertIsInstance(contacts_list.get("contacts"), list)
        self.assertEqual(contacts_view.get("id"), row["id"])
        self.assertIsInstance(contacts_search.get("contacts"), list)

    async def _assert_calendar_tools_when_cached(self, server: object) -> None:
        calendars = await self._call_tool(server, "icloud.calendar.list_calendars", {})
        row = self.db.query_one(
            """
            SELECT id
            FROM calendar_objects
            WHERE deleted_at IS NULL
            ORDER BY dtstart
            LIMIT 1
            """
        )
        self.assertIsInstance(calendars.get("calendars"), list)
        if not row:
            return
        events = await self._call_tool(server, "icloud.calendar.list_events", {"limit": 5})
        event = await self._call_tool(server, "icloud.calendar.view_event", {"event_id": row["id"]})
        event_search = await self._call_tool(
            server, "icloud.calendar.search_events", {"query": self._event_query(row["id"]), "limit": 5}
        )

        self.assertIsInstance(events.get("events"), list)
        self.assertEqual(event.get("id"), row["id"])
        self.assertIn("results", event_search)

    async def _call_tool(self, server: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await server.call_tool(name, arguments)
        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict):
            return structured
        if isinstance(result, dict):
            return result
        self.fail(f"{name} returned unsupported result shape: {type(result)!r}")

    def _count(self, table: str, where: str | None = None) -> int:
        sql = f"SELECT COUNT(*) AS count FROM {table}"
        if where:
            sql += f" WHERE {where}"
        row = self.db.query_one(sql)
        return int(row["count"]) if row else 0

    def _first_search_query(self) -> str:
        row = self.db.query_one(
            """
            SELECT title
            FROM search_documents
            WHERE deleted_at IS NULL AND title IS NOT NULL AND title != ''
            ORDER BY updated_at DESC
            LIMIT 1
            """
        )
        return str(row["title"]) if row else "unlikely-live-smoke-query"

    def _mail_query(self, message_id: str) -> str:
        row = self.db.query_one("SELECT subject FROM mail_messages WHERE id = ?", (message_id,))
        return str(row.get("subject") or "unlikely-live-smoke-query") if row else "unlikely-live-smoke-query"

    def _contact_query(self, row: dict[str, Any]) -> str:
        if row.get("display_name"):
            return str(row["display_name"])
        try:
            emails = json.loads(row.get("emails_json") or "[]")
        except json.JSONDecodeError:
            emails = []
        return str(emails[0]) if emails else "unlikely-live-smoke-query"

    def _event_query(self, event_id: str) -> str:
        row = self.db.query_one("SELECT summary FROM calendar_objects WHERE id = ?", (event_id,))
        return str(row.get("summary") or "unlikely-live-smoke-query") if row else "unlikely-live-smoke-query"


if __name__ == "__main__":
    unittest.main()
