from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from icloud_mcp.config import Settings
from icloud_mcp.db.connection import open_db
from icloud_mcp.db.repositories import ensure_defaults, search_contacts, sync_status, upsert_contact
from icloud_mcp.sync.scheduler import SyncScheduler


class FailingWorker:
    name = "contacts_sync_worker"

    def run_once(self) -> dict:
        raise RuntimeError("temporary failure")


class SlowWorker:
    name = "contacts_sync_worker"

    def run_once(self) -> dict:
        time.sleep(0.2)
        return {"status": "ok"}


class SpecClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(database_path=":memory:", cursor_secret="test-secret", sync_on_start=False)
        self.db = open_db(":memory:")
        ensure_defaults(self.db, self.settings)

    def tearDown(self) -> None:
        self.db.close()

    def test_contacts_search_uses_trigram_and_phone_aliases(self) -> None:
        upsert_contact(
            self.db,
            addressbook_id=self.settings.default_addressbook_id,
            contact_id="contact_1",
            href="local://contacts/1.vcf",
            raw_vcard="BEGIN:VCARD\nFN:Alexandra Hamilton\nTEL:(415) 555-0100\nEND:VCARD",
            display_name="Alexandra Hamilton",
            emails=["alexandra@example.com"],
            phones=["(415) 555-0100"],
            given_name="Alexandra",
            family_name="Hamilton",
        )

        trigram = search_contacts(self.db, "lexandra", limit=10)
        phone = search_contacts(self.db, "+14155550100", limit=10)

        self.assertEqual(trigram["contacts"][0]["id"], "contact_1")
        self.assertEqual(phone["contacts"][0]["id"], "contact_1")
        self.assertEqual(phone["contacts"][0]["phones"], ["(415) 555-0100"])

    def test_worker_backoff_and_dead_letter_are_reported(self) -> None:
        scheduler = SyncScheduler(self.db, self.settings)

        with patch("icloud_mcp.sync.scheduler.LOGGER.exception"):
            first = scheduler._run_worker_with_gate(FailingWorker())
        second = scheduler._run_worker_with_gate(FailingWorker())

        self.assertEqual(first["status"], "error")
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(second["reason"], "backoff_active")
        self.assertEqual(sync_status(self.db)["workers"]["contacts_sync_worker"]["status"], "backoff")

        self.db.execute(
            "UPDATE sync_checkpoints SET retry_count = 5, backoff_until = NULL WHERE name = ?",
            ("contacts_sync_worker",),
        )
        dead = scheduler._run_worker_with_gate(FailingWorker())

        self.assertEqual(dead["status"], "dead_letter")
        self.assertEqual(sync_status(self.db)["workers"]["contacts_sync_worker"]["detail"]["circuit"], "open")

    def test_worker_gate_skips_overlapping_run(self) -> None:
        scheduler = SyncScheduler(self.db, self.settings)
        results: list[dict] = []
        thread = threading.Thread(target=lambda: results.append(scheduler._run_worker_with_gate(SlowWorker())))
        thread.start()
        time.sleep(0.05)

        skipped = scheduler._run_worker_with_gate(SlowWorker())
        thread.join()

        self.assertEqual(skipped["status"], "skipped")
        self.assertEqual(skipped["reason"], "already_running")
        self.assertEqual(results[0]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
