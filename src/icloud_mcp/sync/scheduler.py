"""Background sync scheduler."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from icloud_mcp.calendar.sync import CalendarSyncWorker
from icloud_mcp.contacts.sync import ContactsSyncWorker
from icloud_mcp.mail.sync import MailBackfillWorker, MailSyncWorker
from icloud_mcp.platform.config import Settings
from icloud_mcp.platform.metrics import record_metric
from icloud_mcp.search.embeddings import EmbeddingWorker
from icloud_mcp.search.maintenance import cleanup_local_index
from icloud_mcp.storage.connection import Database
from icloud_mcp.sync.checkpoints import (
    MAX_RETRIES,
    initialize_checkpoints,
    update_checkpoint,
    update_failure_checkpoint,
    update_worker_result_checkpoint,
    update_worker_start_checkpoint,
)

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
    _state_lock: threading.Lock = field(default_factory=threading.Lock)
    _background_running: bool = False
    _last_cycle_started_at: str | None = None
    _last_cycle_finished_at: str | None = None
    _next_run_at: str | None = None

    def initialize_checkpoints(self) -> None:
        """Ensure all known sync workers have dashboard-visible checkpoints."""

        initialize_checkpoints(self.db, WORKERS)

    def start_background(self) -> None:
        """Initialize checkpoints and start sync loop when enabled."""

        self.initialize_checkpoints()
        if not self.settings.sync_on_start:
            return
        with self._state_lock:
            self._background_running = True
            self._next_run_at = datetime.now(tz=UTC).replace(microsecond=0).isoformat()
        thread = threading.Thread(target=self._loop, name="icloud-mcp-sync", daemon=True)
        thread.start()
        self._threads.append(thread)

    def stop(self) -> None:
        """Stop background sync threads."""

        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2)
        with self._state_lock:
            self._background_running = False
            self._next_run_at = None

    def sync_now(self) -> dict:
        """Run contacts, calendar, and mail sync once in priority order."""

        self._mark_cycle_started()
        results: dict[str, dict] = {}
        try:
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
            cleanup = cleanup_local_index(self.db)
            update_checkpoint(
                self.db,
                "maintenance_worker",
                "ok",
                {"expired_query_cache_removed": True, **cleanup},
            )
            return results
        finally:
            self._mark_cycle_finished()

    def status(self) -> dict[str, Any]:
        """Return scheduler lifecycle state for local dashboards."""

        with self._state_lock:
            return {
                "background_running": self._background_running,
                "sync_on_start": self.settings.sync_on_start,
                "sync_interval_seconds": self.settings.sync_interval_seconds,
                "last_cycle_started_at": self._last_cycle_started_at,
                "last_cycle_finished_at": self._last_cycle_finished_at,
                "next_run_at": self._next_run_at,
            }

    def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                with self._state_lock:
                    self._next_run_at = None
                try:
                    self.sync_now()
                except Exception as exc:
                    LOGGER.exception("Background sync cycle failed")
                    record_metric(self.db, "sync.loop_failure", 1, {"error": exc.__class__.__name__})
                    update_checkpoint(
                        self.db,
                        "maintenance_worker",
                        "error",
                        {"status": "error", "reason": "background_loop", "error": exc.__class__.__name__},
                    )
                wait_seconds = max(60, self.settings.sync_interval_seconds)
                with self._state_lock:
                    self._next_run_at = (
                        (datetime.now(tz=UTC) + timedelta(seconds=wait_seconds)).replace(microsecond=0).isoformat()
                    )
                self._stop.wait(wait_seconds)
        finally:
            with self._state_lock:
                self._background_running = False
                self._next_run_at = None

    def _run_worker_with_gate(self, worker: object) -> dict:
        name = worker.name
        lock = _WORKER_LOCKS[name]
        if not lock.acquire(blocking=False):
            result = {"status": "skipped", "reason": "already_running"}
            update_checkpoint(self.db, name, "skipped", result)
            return result
        try:
            checkpoint = self.db.query_one(
                "SELECT retry_count, backoff_until FROM sync_checkpoints WHERE name = ?", (name,)
            )
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
                update_worker_start_checkpoint(self.db, name)
                result = worker.run_once()
                update_worker_result_checkpoint(self.db, name, result)
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

    def _mark_cycle_started(self) -> None:
        now = datetime.now(tz=UTC).replace(microsecond=0).isoformat()
        with self._state_lock:
            self._last_cycle_started_at = now

    def _mark_cycle_finished(self) -> None:
        now = datetime.now(tz=UTC).replace(microsecond=0).isoformat()
        with self._state_lock:
            self._last_cycle_finished_at = now


def _in_backoff(value: str | None) -> bool:
    if not value:
        return False
    try:
        return datetime.fromisoformat(value) > datetime.now(tz=UTC)
    except ValueError:
        return False
