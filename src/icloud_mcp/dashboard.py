"""Local dashboard HTTP runtime for the MCP server."""

from __future__ import annotations

import json
import socket
import threading
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any

from icloud_mcp import __version__
from icloud_mcp.config import Settings
from icloud_mcp.db.connection import Database
from icloud_mcp.db.repositories import sync_status
from icloud_mcp.observability.metrics import metrics_snapshot
from icloud_mcp.sync.scheduler import SyncScheduler
from icloud_mcp.util import utc_now

READ_ANNOTATIONS = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}
DASHBOARD_WRITE_ANNOTATIONS = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
DASHBOARD_HTML = "dashboard.html"


@dataclass
class DashboardRuntime:
    """Own one localhost dashboard server per MCP process."""

    db: Database
    settings: Settings
    scheduler: SyncScheduler
    host: str = "127.0.0.1"
    default_port: int = 8765
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None
    _manual_sync_lock: threading.Lock = field(default_factory=threading.Lock)
    _manual_state_lock: threading.Lock = field(default_factory=threading.Lock)
    _manual_sync_state: dict[str, Any] = field(
        default_factory=lambda: {
            "running": False,
            "last_started_at": None,
            "last_finished_at": None,
            "last_result": None,
        }
    )

    def start(self) -> dict[str, Any]:
        """Start dashboard if needed and return the local URL."""

        self._initialize_worker_status()
        with self._lock:
            if self._server:
                return self._status_unlocked()

            server = self._bind_server()
            thread = threading.Thread(target=server.serve_forever, name="icloud-mcp-dashboard", daemon=True)
            thread.start()
            self._server = server
            self._thread = thread
            return self._status_unlocked()

    def status(self) -> dict[str, Any]:
        """Return dashboard server state."""

        with self._lock:
            return self._status_unlocked()

    def stop(self) -> dict[str, Any]:
        """Stop dashboard server if running."""

        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
        if server:
            server.shutdown()
            server.server_close()
        if thread:
            thread.join(timeout=2)
        return self.status()

    def snapshot(self) -> dict[str, Any]:
        """Build the dashboard operational snapshot from local state."""

        self._initialize_worker_status()
        status = sync_status(self.db, self.settings.stale_after_seconds)
        workers = status.get("workers", {})
        scheduler_status = self.scheduler.status()
        manual_sync = self._manual_sync_snapshot()
        return {
            "generated_at": utc_now(),
            "health": _health(status),
            "activity": _activity(status, scheduler_status, manual_sync),
            "sync": status,
            "scheduler": scheduler_status,
            "manual_sync": manual_sync,
            "metrics": metrics_snapshot(self.db, limit=50),
            "counts": _counts(self.db),
            "info": {
                "version": __version__,
                "database_path": str(self.settings.database_path),
                "sync_on_start": self.settings.sync_on_start,
                "sync_interval_seconds": self.settings.sync_interval_seconds,
                "stale_after_seconds": self.settings.stale_after_seconds,
                "mail_sync_days": self.settings.mail_sync_days,
                "mail_sync_limit_per_mailbox": self.settings.mail_sync_limit_per_mailbox,
                "calendar_past_months": self.settings.calendar_past_months,
                "calendar_future_months": self.settings.calendar_future_months,
                "query_cache_ttl_seconds": self.settings.query_cache_ttl_seconds,
                "attachment_text_indexing": self.settings.attachment_text_indexing,
                "workers": list(workers),
            },
        }

    def sync_now_background(self) -> dict[str, Any]:
        """Start a manual sync in a background thread."""

        if not self._manual_sync_lock.acquire(blocking=False):
            return {"accepted": False, "reason": "already_running", "manual_sync": self._manual_sync_snapshot()}
        self._update_manual_sync_state(running=True, last_started_at=utc_now(), last_finished_at=None, last_result=None)

        def run() -> None:
            try:
                result = self.scheduler.sync_now()
            except Exception as exc:
                result = {"status": "error", "error": exc.__class__.__name__}
            try:
                self._update_manual_sync_state(running=False, last_finished_at=utc_now(), last_result=result)
            finally:
                self._manual_sync_lock.release()

        threading.Thread(target=run, name="icloud-mcp-dashboard-sync", daemon=True).start()
        return {"accepted": True, "manual_sync": self._manual_sync_snapshot()}

    def _initialize_worker_status(self) -> None:
        initializer = getattr(self.scheduler, "initialize_checkpoints", None)
        if callable(initializer):
            initializer()

    def _bind_server(self) -> ThreadingHTTPServer:
        handler = _make_handler(self)
        port = self.default_port
        for candidate in range(port, port + 50):
            try:
                return ThreadingHTTPServer((self.host, candidate), handler)
            except OSError:
                continue
        raise RuntimeError(f"no free localhost dashboard port found starting at {port}")

    def _status_unlocked(self) -> dict[str, Any]:
        running = self._server is not None
        port = self._server.server_address[1] if self._server else None
        url = f"http://{self.host}:{port}/" if port else None
        return {"running": running, "host": self.host, "port": port, "url": url}

    def _manual_sync_snapshot(self) -> dict[str, Any]:
        with self._manual_state_lock:
            return dict(self._manual_sync_state)

    def _update_manual_sync_state(self, **updates: Any) -> None:
        with self._manual_state_lock:
            self._manual_sync_state.update(updates)


def _health(status: dict[str, Any]) -> dict[str, str]:
    freshness = status.get("freshness_status", {})
    domain_states = [str(value.get("status")) for value in freshness.values() if isinstance(value, dict)]
    workers = status.get("workers", {})
    worker_states = [str(value.get("status")) for value in workers.values() if isinstance(value, dict)]
    if any(state in {"error", "dead_letter", "backoff"} for state in worker_states):
        return {"status": "degraded", "reason": "one or more sync workers need attention"}
    if any(state == "never_synced" for state in domain_states):
        return {"status": "not_synced", "reason": "one or more domains have not synced yet"}
    if any(state == "stale" for state in domain_states):
        return {"status": "degraded", "reason": "one or more domains are stale"}
    if any(state == "running" for state in worker_states):
        return {"status": "syncing", "reason": "sync workers are running"}
    return {"status": "healthy", "reason": "local cache is within freshness threshold"}


def _activity(status: dict[str, Any], scheduler_status: dict[str, Any], manual_sync: dict[str, Any]) -> dict[str, Any]:
    workers = status.get("workers", {})
    running_workers = sorted(
        name for name, worker in workers.items() if isinstance(worker, dict) and worker.get("status") == "running"
    )
    attention_workers = sorted(
        name
        for name, worker in workers.items()
        if isinstance(worker, dict) and worker.get("status") in {"error", "dead_letter", "backoff"}
    )
    return {
        "live": bool(running_workers or manual_sync.get("running")),
        "running_workers": running_workers,
        "attention_workers": attention_workers,
        "next_run_at": scheduler_status.get("next_run_at"),
        "last_cycle_started_at": scheduler_status.get("last_cycle_started_at"),
        "last_cycle_finished_at": scheduler_status.get("last_cycle_finished_at"),
        "manual_sync_running": bool(manual_sync.get("running")),
    }


def _counts(db: Database) -> dict[str, int]:
    queries = {
        "mail_messages": "SELECT COUNT(*) AS value FROM mail_messages WHERE deleted_at IS NULL",
        "calendar_events": "SELECT COUNT(*) AS value FROM calendar_objects WHERE deleted_at IS NULL",
        "contacts": "SELECT COUNT(*) AS value FROM contacts WHERE deleted_at IS NULL",
        "search_documents": "SELECT COUNT(*) AS value FROM search_documents WHERE deleted_at IS NULL",
        "search_chunks": "SELECT COUNT(*) AS value FROM search_chunks",
        "pending_embeddings": "SELECT COUNT(*) AS value FROM search_chunks WHERE embedding_status = 'pending'",
        "query_cache": "SELECT COUNT(*) AS value FROM query_cache",
    }
    counts: dict[str, int] = {}
    for name, sql in queries.items():
        row = db.query_one(sql)
        counts[name] = int((row or {}).get("value") or 0)
    return counts


def _make_handler(runtime: DashboardRuntime) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path in {"/", "/dashboard"}:
                self._send_html(_dashboard_html())
                return
            if self.path == "/api/status":
                self._send_json(runtime.snapshot())
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            if self.path == "/api/sync-now":
                self._send_json(runtime.sync_now_background(), status=HTTPStatus.ACCEPTED)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def log_message(self, format: str, *args: object) -> None:
            return

        def _send_html(self, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return DashboardHandler


def _dashboard_html() -> str:
    """Read dashboard HTML from disk so UI edits are visible after browser refresh."""

    return resources.files("icloud_mcp").joinpath(DASHBOARD_HTML).read_text(encoding="utf-8")


def localhost_port_available(host: str, port: int) -> bool:
    """Return whether a local TCP port can be bound."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True
