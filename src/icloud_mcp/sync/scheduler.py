"""Background sync scheduler."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from icloud_mcp.config import Settings
from icloud_mcp.db.connection import Database
from icloud_mcp.indexing.embeddings import EmbeddingWorker
from icloud_mcp.observability.metrics import record_metric
from icloud_mcp.security.redaction import redact_text
from icloud_mcp.sync.calendar_sync import CalendarSyncWorker
from icloud_mcp.sync.checkpoints import update_checkpoint
from icloud_mcp.sync.contacts_sync import ContactsSyncWorker
from icloud_mcp.sync.mail_sync import MailSyncWorker
from icloud_mcp.util import compact_json

LOGGER = logging.getLogger(__name__)

WORKERS = [
    "mail_sync_worker",
    "calendar_sync_worker",
    "contacts_sync_worker",
    "indexer_worker",
    "embedding_worker",
    "maintenance_worker",
]


@dataclass
class SyncScheduler:
    """Runs credential-backed sync cycles without blocking MCP calls."""

    db: Database
    settings: Settings
    _stop: threading.Event = field(default_factory=threading.Event)
    _threads: list[threading.Thread] = field(default_factory=list)

    def start_background(self) -> None:
        """Initialize checkpoints and start sync loop when enabled."""

        for worker in WORKERS:
            self.db.execute(
                """
                INSERT INTO sync_checkpoints (name, status, last_sync_at, detail_json, retry_count)
                VALUES (?, 'idle', NULL, ?, 0)
                ON CONFLICT(name) DO NOTHING
                """,
                (worker, compact_json({"mode": "ready"})),
            )
        if not self.settings.sync_on_start:
            return
        thread = threading.Thread(target=self._loop, name="icloud-mcp-sync", daemon=True)
        thread.start()
        self._threads.append(thread)

    def stop(self) -> None:
        """Stop background sync threads."""

        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2)

    def sync_now(self) -> dict:
        """Run contacts, calendar, and mail sync once in priority order."""

        results: dict[str, dict] = {}
        for worker in [
            ContactsSyncWorker(self.db, self.settings),
            CalendarSyncWorker(self.db, self.settings),
            MailSyncWorker(self.db, self.settings),
        ]:
            started = time.perf_counter()
            try:
                update_checkpoint(self.db, worker.name, "running", {"mode": "manual_or_background"})
                results[worker.name] = worker.run_once()
                record_metric(
                    self.db, "sync.duration_ms", (time.perf_counter() - started) * 1000, {"worker": worker.name}
                )
            except Exception as exc:
                LOGGER.exception("Sync worker failed: %s", worker.name)
                failure = {
                    "status": "error",
                    "error": exc.__class__.__name__,
                    "message": redact_text(str(exc), allow_unredacted=self.settings.allow_unredacted_debug),
                    "last_error": exc.__class__.__name__,
                    "retry_count": 1,
                }
                update_checkpoint(self.db, worker.name, "error", failure)
                record_metric(self.db, "sync.failure", 1, {"worker": worker.name, "error": exc.__class__.__name__})
                results[worker.name] = failure
        update_checkpoint(self.db, "indexer_worker", "ok", {"mode": "inline_fts"})
        results["embedding_worker"] = EmbeddingWorker(self.db).run_once()
        self.db.execute("DELETE FROM query_cache WHERE expires_at < datetime('now')")
        update_checkpoint(self.db, "maintenance_worker", "ok", {"expired_query_cache_removed": True})
        return results

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.sync_now()
            self._stop.wait(max(60, self.settings.sync_interval_seconds))
