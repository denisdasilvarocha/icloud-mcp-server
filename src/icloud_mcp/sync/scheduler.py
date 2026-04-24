"""Background sync scheduler."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from icloud_mcp.config import Settings
from icloud_mcp.db.connection import Database
from icloud_mcp.indexing.embeddings import EmbeddingWorker
from icloud_mcp.observability.metrics import record_metric
from icloud_mcp.sync.calendar_sync import CalendarSyncWorker
from icloud_mcp.sync.checkpoints import MAX_RETRIES, update_checkpoint, update_failure_checkpoint
from icloud_mcp.sync.contacts_sync import ContactsSyncWorker
from icloud_mcp.sync.mail_sync import MailBackfillWorker, MailSyncWorker
from icloud_mcp.util import compact_json

LOGGER = logging.getLogger(__name__)
WORKERS = [
    "mail_sync_worker",
    "mail_backfill_worker",
    "calendar_sync_worker",
    "contacts_sync_worker",
    "indexer_worker",
    "embedding_worker",
    "maintenance_worker",
]
_WORKER_LOCKS = {name: threading.Lock() for name in WORKERS}


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
            MailBackfillWorker(self.db, self.settings),
        ]:
            results[worker.name] = self._run_worker_with_gate(worker)
        update_checkpoint(self.db, "indexer_worker", "ok", {"mode": "inline_fts"})
        results["embedding_worker"] = EmbeddingWorker(self.db).run_once()
        self.db.execute("DELETE FROM query_cache WHERE expires_at < datetime('now')")
        update_checkpoint(self.db, "maintenance_worker", "ok", {"expired_query_cache_removed": True})
        return results

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.sync_now()
            self._stop.wait(max(60, self.settings.sync_interval_seconds))

    def _run_worker_with_gate(self, worker: object) -> dict:
        name = worker.name
        lock = _WORKER_LOCKS[name]
        if not lock.acquire(blocking=False):
            result = {"status": "skipped", "reason": "already_running"}
            update_checkpoint(self.db, name, "skipped", result)
            return result
        try:
            checkpoint = self.db.query_one("SELECT retry_count, backoff_until FROM sync_checkpoints WHERE name = ?", (name,))
            if checkpoint and int(checkpoint.get("retry_count") or 0) >= MAX_RETRIES:
                result = {
                    "status": "dead_letter",
                    "reason": "max_retries_exceeded",
                    "retry_count": checkpoint.get("retry_count") or 0,
                    "circuit": "open",
                }
                update_checkpoint(self.db, name, "dead_letter", result)
                return result
            if checkpoint and _in_backoff(checkpoint.get("backoff_until")):
                result = {
                    "status": "skipped",
                    "reason": "backoff_active",
                    "retry_count": checkpoint.get("retry_count") or 0,
                    "backoff_until": checkpoint.get("backoff_until"),
                }
                update_checkpoint(self.db, name, "backoff", result)
                return result

            started = time.perf_counter()
            try:
                update_checkpoint(self.db, name, "running", {"mode": "manual_or_background"})
                result = worker.run_once()
                if result.get("status") not in {"error", "dead_letter", "backoff"}:
                    result["retry_count"] = 0
                checkpoint_status = _checkpoint_status(result.get("status"))
                update_checkpoint(self.db, name, checkpoint_status, result)
                record_metric(self.db, "sync.duration_ms", (time.perf_counter() - started) * 1000, {"worker": name})
                return result
            except Exception as exc:
                LOGGER.exception("Sync worker failed: %s", name)
                failure = update_failure_checkpoint(
                    self.db,
                    name,
                    exc,
                    allow_unredacted=self.settings.allow_unredacted_debug,
                )
                record_metric(self.db, "sync.failure", 1, {"worker": name, "error": exc.__class__.__name__})
                return failure
        finally:
            lock.release()


def _in_backoff(value: str | None) -> bool:
    if not value:
        return False
    try:
        return datetime.fromisoformat(value) > datetime.now(tz=UTC)
    except ValueError:
        return False


def _checkpoint_status(status: object) -> str:
    if status in {"skipped", "error", "dead_letter", "backoff"}:
        return str(status)
    return "ok"
