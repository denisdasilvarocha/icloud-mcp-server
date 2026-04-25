from __future__ import annotations

import asyncio
import io
import json
import unittest
from unittest.mock import patch

from icloud_mcp.config import Settings
from icloud_mcp.dashboard import DashboardRuntime, _dashboard_html, _health, _make_handler, localhost_port_available
from icloud_mcp.db.connection import open_db
from icloud_mcp.db.repositories import ensure_defaults
from icloud_mcp.server import create_server
from icloud_mcp.sync.checkpoints import update_checkpoint
from icloud_mcp.sync.scheduler import SyncScheduler


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(database_path=":memory:", cursor_secret="dashboard-secret", sync_on_start=False)
        self.db = open_db(":memory:")
        ensure_defaults(self.db, self.settings)
        self.scheduler = _FakeScheduler()
        self.dashboard = DashboardRuntime(self.db, self.settings, self.scheduler)

    def tearDown(self) -> None:
        self.dashboard.stop()
        self.db.close()

    def test_dashboard_tools_start_status_stop(self) -> None:
        fake_server = _FakeHTTPServer(("127.0.0.1", 8765), object)

        async def run() -> dict[str, object]:
            server = create_server(self.settings, self.db, scheduler=self.scheduler, dashboard=self.dashboard)
            tools = {tool.name: tool for tool in await server.list_tools()}
            with patch("icloud_mcp.dashboard.ThreadingHTTPServer", return_value=fake_server):
                first_start = await server.call_tool("icloud.dashboard.start", {})
                second_start = await server.call_tool("icloud.dashboard.start", {})
                status = await server.call_tool("icloud.dashboard.status", {})
                stopped = await server.call_tool("icloud.dashboard.stop", {})
            return {
                "tool_names": set(tools),
                "first": first_start.structured_content,
                "second": second_start.structured_content,
                "status": status.structured_content,
                "stopped": stopped.structured_content,
            }

        result = asyncio.run(run())

        self.assertIn("icloud.dashboard.start", result["tool_names"])
        self.assertIn("icloud.dashboard.status", result["tool_names"])
        self.assertIn("icloud.dashboard.stop", result["tool_names"])
        self.assertTrue(result["first"]["running"])
        self.assertEqual(result["first"]["url"], result["second"]["url"])
        self.assertEqual(result["status"]["url"], result["first"]["url"])
        self.assertFalse(result["stopped"]["running"])
        self.assertTrue(fake_server.closed)

    def test_dashboard_snapshot_and_manual_sync(self) -> None:
        status = self.dashboard.snapshot()
        sync = self.dashboard.sync_now_background()

        for _ in range(20):
            status_after_sync = self.dashboard.snapshot()
            if not status_after_sync["manual_sync"]["running"]:
                break

        self.assertEqual(status["health"]["status"], "not_synced")
        self.assertTrue(sync["accepted"])
        self.assertEqual(status_after_sync["manual_sync"]["last_result"], {"fake_worker": {"status": "ok"}})

    def test_manual_sync_already_running_and_error_result(self) -> None:
        self.dashboard._manual_sync_lock.acquire()
        try:
            skipped = self.dashboard.sync_now_background()
        finally:
            self.dashboard._manual_sync_lock.release()
        failing = DashboardRuntime(self.db, self.settings, _FailingScheduler())
        accepted = failing.sync_now_background()

        for _ in range(20):
            status = failing.snapshot()
            if not status["manual_sync"]["running"]:
                break

        self.assertFalse(skipped["accepted"])
        self.assertEqual(skipped["reason"], "already_running")
        self.assertTrue(accepted["accepted"])
        self.assertEqual(status["manual_sync"]["last_result"], {"status": "error", "error": "RuntimeError"})

    def test_dashboard_port_fallback_and_localhost_probe(self) -> None:
        blocked_port = 8765
        servers = []

        def fake_server(address: tuple[str, int], handler: object) -> _FakeHTTPServer:
            if address[1] == blocked_port:
                raise OSError("occupied")
            server = _FakeHTTPServer(address, handler)
            servers.append(server)
            return server

        dashboard = DashboardRuntime(self.db, self.settings, self.scheduler, default_port=blocked_port)
        try:
            with patch("icloud_mcp.dashboard.ThreadingHTTPServer", side_effect=fake_server):
                started = dashboard.start()
            self.assertTrue(started["running"])
            self.assertEqual(started["port"], blocked_port + 1)
            with patch("icloud_mcp.dashboard.socket.socket", return_value=_FakeSocket()):
                self.assertTrue(localhost_port_available("127.0.0.1", 0))
            with patch("icloud_mcp.dashboard.socket.socket", return_value=_FakeSocket(should_raise=True)):
                self.assertFalse(localhost_port_available("127.0.0.1", 0))
        finally:
            dashboard.stop()
        self.assertTrue(servers[0].closed)

    def test_dashboard_port_exhaustion(self) -> None:
        with (
            patch("icloud_mcp.dashboard.ThreadingHTTPServer", side_effect=OSError("occupied")),
            self.assertRaisesRegex(RuntimeError, "no free localhost dashboard port"),
        ):
            self.dashboard.start()

    def test_handler_routes_and_response_writers(self) -> None:
        handler_type = _make_handler(self.dashboard)
        calls = []

        handler = object.__new__(handler_type)
        handler._send_html = lambda html: calls.append(("html", "iCloud MCP Dashboard" in html))
        handler._send_json = lambda payload, status=None: calls.append(("json", payload, status))
        handler.send_error = lambda status: calls.append(("error", status))
        handler.path = "/"
        handler.do_GET()
        handler.path = "/api/status"
        handler.do_GET()
        handler.path = "/missing"
        handler.do_GET()
        handler.path = "/api/sync-now"
        handler.do_POST()
        handler.path = "/missing"
        handler.do_POST()
        handler.log_message("ignored")

        writer = object.__new__(handler_type)
        writer.responses = []
        writer.headers = []
        writer.wfile = io.BytesIO()
        writer.send_response = lambda status: writer.responses.append(status)
        writer.send_header = lambda name, value: writer.headers.append((name, value))
        writer.end_headers = lambda: None
        handler_type._send_html(writer, "ok")
        handler_type._send_json(writer, {"ok": True})

        self.assertEqual(calls[0], ("html", True))
        self.assertEqual(calls[1][0], "json")
        self.assertEqual(calls[2][0], "error")
        self.assertEqual(calls[3][0], "json")
        self.assertEqual(calls[4][0], "error")
        self.assertIn(b"ok", writer.wfile.getvalue())

    def test_scheduler_status(self) -> None:
        scheduler = SyncScheduler(self.db, self.settings)
        dashboard = DashboardRuntime(self.db, self.settings, scheduler)
        snapshot = dashboard.snapshot()
        status = scheduler.status()

        self.assertFalse(status["background_running"])
        self.assertEqual(status["sync_interval_seconds"], self.settings.sync_interval_seconds)
        self.assertIn("mail_sync_worker", snapshot["sync"]["workers"])

    def test_snapshot_health_and_counts(self) -> None:
        update_checkpoint(self.db, "mail_sync_worker", "error", {"error": "RuntimeError", "retry_count": 1})

        snapshot = self.dashboard.snapshot()

        self.assertEqual(snapshot["health"]["status"], "degraded")
        self.assertEqual(snapshot["counts"]["mail_messages"], 0)
        self.assertIn("sync_interval_seconds", snapshot["info"])
        self.assertNotIn("app_password", json.dumps(snapshot))

    def test_health_classification_edges(self) -> None:
        self.assertEqual(_health({"freshness_status": {}, "workers": {"x": {"status": "running"}}})["status"], "syncing")
        self.assertEqual(_health({"freshness_status": {"mail": {"status": "stale"}}, "workers": {}})["status"], "degraded")
        self.assertEqual(_health({"freshness_status": {"mail": {"status": "healthy"}}, "workers": {}})["status"], "healthy")

    def test_dashboard_renders_worker_progress_bar(self) -> None:
        html = _dashboard_html()

        self.assertIn("const progressBar = (worker)", html)
        self.assertNotIn('text(worker.progress_cursor, "N/A")', html)


class _FakeScheduler:
    def __init__(self) -> None:
        self.calls = 0

    def status(self) -> dict[str, object]:
        return {
            "background_running": False,
            "sync_on_start": False,
            "sync_interval_seconds": 900,
            "last_cycle_started_at": None,
            "last_cycle_finished_at": None,
            "next_run_at": None,
        }

    def sync_now(self) -> dict[str, dict[str, str]]:
        self.calls += 1
        return {"fake_worker": {"status": "ok"}}


class _FailingScheduler(_FakeScheduler):
    def sync_now(self) -> dict[str, dict[str, str]]:
        raise RuntimeError("boom")


class _FakeHTTPServer:
    def __init__(self, address: tuple[str, int], handler: object) -> None:
        self.server_address = address
        self.handler = handler
        self.closed = False

    def serve_forever(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def server_close(self) -> None:
        self.closed = True


class _FakeSocket:
    def __init__(self, should_raise: bool = False) -> None:
        self.should_raise = should_raise

    def __enter__(self) -> _FakeSocket:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def bind(self, address: tuple[str, int]) -> None:
        if self.should_raise:
            raise OSError("occupied")
        return None


if __name__ == "__main__":
    unittest.main()
