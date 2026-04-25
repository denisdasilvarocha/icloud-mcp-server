"""Local dashboard HTTP runtime for the MCP server."""

from __future__ import annotations

import json
import socket
import threading
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from icloud_mcp import __version__
from icloud_mcp.platform.config import Settings
from icloud_mcp.platform.metrics import metrics_snapshot
from icloud_mcp.platform.util import utc_now
from icloud_mcp.storage.cache_state import sync_status
from icloud_mcp.storage.connection import Database
from icloud_mcp.sync.scheduler import SyncScheduler

READ_ANNOTATIONS = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}
DASHBOARD_WRITE_ANNOTATIONS = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
DASHBOARD_HTML = r"""<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>iCloud MCP Dashboard</title>
<script src="https://code.iconify.design/iconify-icon/1.0.7/iconify-icon.min.js"></script>
<script src="https://cdn.tailwindcss.com"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin="">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&amp;display=swap" rel="stylesheet">
<style>
.scrollbar-hide::-webkit-scrollbar { display: none; }
.scrollbar-hide { -ms-overflow-style: none; scrollbar-width: none; }
</style>
</head>
<body class="bg-slate-50 text-slate-900 font-sans antialiased text-sm min-h-screen selection:bg-blue-100 selection:text-blue-900">

  <nav class="bg-white border-b border-slate-200 sticky top-0 z-50">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
      <div class="flex items-center justify-between h-14">
        <div class="flex items-center gap-3">
          <span class="font-medium tracking-tighter text-lg text-slate-900">iCloud MCP Server</span>
        </div>
        <div class="flex items-center gap-4">
          <div class="flex items-center gap-2 text-xs text-slate-500">
            <span class="relative flex h-2 w-2">
              <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-500 opacity-75"></span>
              <span class="relative inline-flex rounded-full h-2 w-2 bg-emerald-500"></span>
            </span>
            <span id="updatedTime">Loading...</span>
          </div>
        </div>
      </div>
    </div>
  </nav>

  <main class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-8">

    <header class="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
      <div>
        <h1 class="text-2xl sm:text-3xl font-medium tracking-tight text-slate-900">Overview</h1>
        <p class="text-sm text-slate-500 mt-1">Manage and monitor your iCloud sync status.</p>
      </div>
      <div class="flex items-center gap-3">
        <button id="refreshBtn" class="flex items-center gap-2 rounded-md border border-slate-200 bg-white px-4 py-2 text-sm font-normal text-slate-700 hover:bg-slate-50 hover:text-slate-900 transition-colors shadow-sm">
          <iconify-icon icon="solar:refresh-linear" stroke-width="1.5"></iconify-icon>
          Refresh
        </button>
        <button id="syncBtn" class="flex items-center gap-2 rounded-md bg-slate-900 px-4 py-2 text-sm font-normal text-white shadow-sm hover:bg-slate-800 transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
          <iconify-icon icon="solar:cloud-upload-linear" stroke-width="1.5"></iconify-icon>
          Sync Data
        </button>
      </div>
    </header>

    <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
      <div class="bg-white p-5 rounded-lg border border-slate-200 shadow-sm flex flex-col gap-3">
        <div class="flex items-center justify-between text-slate-500">
          <span class="text-xs font-medium uppercase tracking-wide">System Health</span>
          <iconify-icon icon="solar:shield-check-linear" class="text-lg" stroke-width="1.5" id="healthIcon"></iconify-icon>
        </div>
        <div>
          <div class="text-xl font-medium text-slate-900 tracking-tight capitalize" id="healthStatusTitle">Loading</div>
          <div class="text-xs text-slate-500 mt-1 truncate" id="healthReasonText">Initializing...</div>
        </div>
      </div>

      <div class="bg-white p-5 rounded-lg border border-slate-200 shadow-sm flex flex-col gap-3">
        <div class="flex items-center justify-between text-slate-500">
          <span class="text-xs font-medium uppercase tracking-wide">Live Activity</span>
          <iconify-icon icon="solar:pulse-linear" class="text-lg" stroke-width="1.5"></iconify-icon>
        </div>
        <div>
          <div class="text-xl font-medium text-slate-900 tracking-tight" id="liveActivityStatus">Loading</div>
          <div class="text-xs text-slate-500 mt-1 truncate" id="activeWorkersText">Waiting for data...</div>
        </div>
      </div>

      <div class="bg-white p-5 rounded-lg border border-slate-200 shadow-sm flex flex-col gap-3">
        <div class="flex items-center justify-between text-slate-500">
          <span class="text-xs font-medium uppercase tracking-wide">Total Items</span>
          <iconify-icon icon="solar:database-linear" class="text-lg" stroke-width="1.5"></iconify-icon>
        </div>
        <div>
          <div class="text-xl font-medium text-slate-900 tracking-tight" id="totalItemsCount">0</div>
          <div class="text-xs text-slate-500 mt-1 truncate">Across all collections</div>
        </div>
      </div>

      <div class="bg-white p-5 rounded-lg border border-slate-200 shadow-sm flex flex-col gap-3">
        <div class="flex items-center justify-between text-slate-500">
          <span class="text-xs font-medium uppercase tracking-wide">Next Sync</span>
          <iconify-icon icon="solar:clock-circle-linear" class="text-lg" stroke-width="1.5"></iconify-icon>
        </div>
        <div>
          <div class="text-xl font-medium text-slate-900 tracking-tight" id="nextSyncTime">Loading</div>
          <div class="text-xs text-slate-500 mt-1 truncate" id="backgroundStatus">Scheduler status</div>
        </div>
      </div>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-3 gap-8">

      <div class="lg:col-span-2 space-y-8">

        <section class="bg-white rounded-lg border border-slate-200 shadow-sm overflow-hidden">
          <div class="px-5 py-4 border-b border-slate-100 flex items-center justify-between bg-slate-50/50">
            <h2 class="text-sm font-medium text-slate-900">Worker Processes</h2>
          </div>
          <div class="overflow-x-auto scrollbar-hide">
            <table class="w-full text-left whitespace-nowrap">
              <thead class="bg-white text-xs text-slate-500 border-b border-slate-100">
                <tr>
                  <th class="px-5 py-3 font-normal">Service</th>
                  <th class="px-5 py-3 font-normal">Status</th>
                  <th class="px-5 py-3 font-normal">Last Active</th>
                  <th class="px-5 py-3 font-normal">Retries</th>
                  <th class="px-5 py-3 font-normal">Progress</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-slate-100" id="workersTableBody"></tbody>
            </table>
          </div>
        </section>

        <section class="bg-white rounded-lg border border-slate-200 shadow-sm overflow-hidden">
          <div class="px-5 py-4 border-b border-slate-100 bg-slate-50/50">
            <h2 class="text-sm font-medium text-slate-900">Local Cache</h2>
          </div>
          <div class="p-0">
            <ul class="divide-y divide-slate-100" id="cacheList"></ul>
          </div>
        </section>

      </div>

      <div class="lg:col-span-1 space-y-8">

        <section class="bg-white rounded-lg border border-slate-200 shadow-sm overflow-hidden">
          <div class="px-5 py-4 border-b border-slate-100 bg-slate-50/50">
            <h2 class="text-sm font-medium text-slate-900">Data Freshness</h2>
          </div>
          <div class="p-5 flex flex-col gap-4" id="freshnessList"></div>
        </section>

        <section class="bg-white rounded-lg border border-slate-200 shadow-sm overflow-hidden">
          <div class="px-5 py-4 border-b border-slate-100 bg-slate-50/50">
            <h2 class="text-sm font-medium text-slate-900">Metrics &amp; Info</h2>
          </div>
          <div class="p-0">
            <ul class="divide-y divide-slate-100" id="infoMetricsList"></ul>
          </div>
        </section>

      </div>

    </div>
  </main>

  <script>
    const getStatusColor = (status) => {
      const s = status ? status.toLowerCase() : "";
      if (s === "healthy" || s === "ok" || s === "idle") return { text: "text-emerald-600", dot: "bg-emerald-500", icon: "solar:check-circle-linear" };
      if (s === "error" || s === "dead_letter" || s === "bad") return { text: "text-red-600", dot: "bg-red-500", icon: "solar:close-circle-linear" };
      return { text: "text-amber-600", dot: "bg-amber-500", icon: "solar:refresh-circle-linear" };
    };

    const text = (val, empty = "None") => val == null || val === "" ? empty : val;
    const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;"
    }[char]));
    const when = (val, empty = "Not scheduled") => val ? new Date(val).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : empty;
    const age = (sec) => sec == null ? "Never" : `${Math.round(sec / 60)}m ago`;
    const formatName = (str) => str.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());

    const listItem = (label, value) => `
      <li class="flex items-center justify-between px-5 py-3 hover:bg-slate-50 transition-colors">
        <span class="text-slate-500 text-sm">${escapeHtml(label)}</span>
        <span class="font-normal text-slate-900 text-sm text-right">${escapeHtml(text(value))}</span>
      </li>`;
    const progressBar = (worker) => {
      const status = worker.status ? worker.status.toLowerCase() : "";
      const label = text(worker.progress_cursor, status || "idle");
      let bar = { width: "20%", color: "bg-slate-300", track: "bg-slate-100", pulse: "" };
      if (status === "ok") bar = { width: "100%", color: "bg-emerald-500", track: "bg-emerald-100", pulse: "" };
      if (status === "running") bar = { width: "55%", color: "bg-emerald-500", track: "bg-emerald-100", pulse: " animate-pulse" };
      if (status === "skipped" || status === "idle") bar = { width: "12%", color: "bg-slate-300", track: "bg-slate-100", pulse: "" };
      if (status === "backoff") bar = { width: "25%", color: "bg-amber-500", track: "bg-amber-100", pulse: "" };
      if (status === "error" || status === "dead_letter") bar = { width: "18%", color: "bg-red-500", track: "bg-red-100", pulse: "" };
      return `
        <div class="w-28" title="${escapeHtml(label)}">
          <div class="h-1.5 rounded-full ${bar.track} overflow-hidden">
            <div class="h-full rounded-full ${bar.color}${bar.pulse}" style="width: ${bar.width}"></div>
          </div>
        </div>`;
    };

    async function loadStatus() {
      const response = await fetch("/api/status", { cache: "no-store" });
      if (!response.ok) {
        throw new Error(`Status request failed: ${response.status}`);
      }
      render(await response.json());
    }

    function render(data) {
      document.getElementById("updatedTime").textContent = new Date(data.generated_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

      // Top Cards
      const hStyle = getStatusColor(data.health.status);
      document.getElementById("healthStatusTitle").textContent = data.health.status;
      document.getElementById("healthStatusTitle").className = `text-xl font-medium tracking-tight capitalize ${hStyle.text}`;
      document.getElementById("healthReasonText").textContent = text(data.health.reason);

      let hIcon = document.getElementById("healthIcon");
      hIcon.setAttribute("icon", hStyle.icon);
      hIcon.className = `text-lg ${hStyle.text}`;

      document.getElementById("liveActivityStatus").textContent = data.activity.live ? "Active" : "Idle";
      document.getElementById("activeWorkersText").textContent = data.activity.running_workers.length > 0 ? `Running: ${data.activity.running_workers.map(w=>w.split('_')[0]).join(', ')}` : "No workers running";

      const totalItems = Object.values(data.counts).reduce((a, b) => a + b, 0);
      document.getElementById("totalItemsCount").textContent = new Intl.NumberFormat().format(totalItems);

      document.getElementById("nextSyncTime").textContent = data.scheduler.next_run_at ? new Date(data.scheduler.next_run_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : "None";
      document.getElementById("backgroundStatus").textContent = data.scheduler.background_running ? "Background scheduler active" : "Scheduler paused";

      document.getElementById("syncBtn").disabled = Boolean(data.manual_sync.running);
      if(data.manual_sync.running) {
        document.getElementById("syncBtn").innerHTML = `<iconify-icon icon="solar:spinner-linear" class="animate-spin" stroke-width="1.5"></iconify-icon> Syncing...`;
      } else {
        document.getElementById("syncBtn").innerHTML = `<iconify-icon icon="solar:cloud-upload-linear" stroke-width="1.5"></iconify-icon> Sync Data`;
      }

      // Workers Table
      document.getElementById("workersTableBody").innerHTML = Object.entries(data.sync.workers).map(([name, worker]) => {
        const style = getStatusColor(worker.status);
        return `
        <tr class="hover:bg-slate-50 transition-colors">
          <td class="px-5 py-3 text-sm text-slate-900 font-normal">${escapeHtml(formatName(name.replace('_sync_worker', '')))}</td>
          <td class="px-5 py-3">
             <div class="flex items-center gap-2">
               <div class="w-1.5 h-1.5 rounded-full ${style.dot}"></div>
               <span class="text-xs font-normal capitalize text-slate-700">${escapeHtml(worker.status)}</span>
             </div>
          </td>
          <td class="px-5 py-3 text-sm text-slate-500">${escapeHtml(when(worker.last_sync_at, "Never"))}</td>
          <td class="px-5 py-3 text-sm text-slate-500">${escapeHtml(worker.retry_count || 0)}</td>
          <td class="px-5 py-3">${progressBar(worker)}</td>
        </tr>
      `}).join("");

      // Freshness
      document.getElementById("freshnessList").innerHTML = Object.entries(data.sync.freshness_status).map(([name, item]) => {
        const style = getStatusColor(item.status);
        return `
          <div class="flex items-start justify-between">
            <div class="flex items-center gap-3">
               <iconify-icon icon="${style.icon}" class="text-lg ${style.text}"></iconify-icon>
               <div>
                 <div class="font-normal text-slate-900 text-sm">${escapeHtml(formatName(name))}</div>
                 <div class="text-xs text-slate-500 mt-0.5">${escapeHtml(age(item.age_seconds))}</div>
               </div>
            </div>
            <span class="text-xs capitalize text-slate-500">${escapeHtml(item.status)}</span>
          </div>
        `;
      }).join("");

      // Cache
      document.getElementById("cacheList").innerHTML = Object.entries(data.counts).map(([name, value]) => listItem(formatName(name), new Intl.NumberFormat().format(value))).join("");

      // Metrics & Info combined
      const combinedInfo = [
        ...Object.entries(data.metrics.totals).map(([k,v]) => [formatName(k), new Intl.NumberFormat().format(v)]),
        ...Object.entries(data.info).map(([k,v]) => [formatName(k), v])
      ];
      document.getElementById("infoMetricsList").innerHTML = combinedInfo.map(([k,v]) => listItem(k, v)).join("");
    }

    document.getElementById("syncBtn").addEventListener("click", async () => {
      const syncBtn = document.getElementById("syncBtn");
      syncBtn.disabled = true;
      try {
        const response = await fetch("/api/sync-now", { method: "POST" });
        if (!response.ok) {
          throw new Error(`Sync request failed: ${response.status}`);
        }
        await loadStatus();
      } catch (error) {
        console.error(error);
        syncBtn.disabled = false;
      }
    });

    document.getElementById("refreshBtn").addEventListener("click", () => {
      loadStatus().catch(console.error);
    });

    loadStatus().catch(console.error);
    setInterval(() => loadStatus().catch(console.error), 5000);
  </script>


</body></html>
"""


@dataclass
class DashboardSnapshotPresenter:
    """Shape dashboard snapshot data for HTTP and MCP status consumers."""

    settings: Settings
    version: str = __version__

    def snapshot(
        self,
        *,
        generated_at: str,
        status: dict[str, Any],
        scheduler_status: dict[str, Any],
        manual_sync: dict[str, Any],
        metrics: dict[str, Any],
        counts: dict[str, int],
    ) -> dict[str, Any]:
        """Build the dashboard operational snapshot from collected local state."""

        workers = status.get("workers", {})
        return {
            "generated_at": generated_at,
            "health": self.health(status),
            "activity": self.activity(status, scheduler_status, manual_sync),
            "sync": status,
            "scheduler": scheduler_status,
            "manual_sync": manual_sync,
            "metrics": metrics,
            "counts": counts,
            "info": {
                "version": self.version,
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

    @staticmethod
    def health(status: dict[str, Any]) -> dict[str, str]:
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

    @staticmethod
    def activity(
        status: dict[str, Any], scheduler_status: dict[str, Any], manual_sync: dict[str, Any]
    ) -> dict[str, Any]:
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
        scheduler_status = self.scheduler.status()
        manual_sync = self._manual_sync_snapshot()
        return DashboardSnapshotPresenter(self.settings).snapshot(
            generated_at=utc_now(),
            status=status,
            scheduler_status=scheduler_status,
            manual_sync=manual_sync,
            metrics=metrics_snapshot(self.db, limit=50),
            counts=_counts(self.db),
        )

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
    return DashboardSnapshotPresenter.health(status)


def _activity(status: dict[str, Any], scheduler_status: dict[str, Any], manual_sync: dict[str, Any]) -> dict[str, Any]:
    return DashboardSnapshotPresenter.activity(status, scheduler_status, manual_sync)


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
    """Return the bundled local dashboard HTML."""

    return DASHBOARD_HTML


def localhost_port_available(host: str, port: int) -> bool:
    """Return whether a local TCP port can be bound."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True
