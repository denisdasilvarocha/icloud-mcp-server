from __future__ import annotations

import asyncio
import unittest

from icloud_mcp.config import Settings
from icloud_mcp.db.connection import open_db
from icloud_mcp.db.repositories import upsert_search_document
from icloud_mcp.server import create_server
from icloud_mcp.services.search import SearchService


class MCPContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = open_db(":memory:")
        self.server = create_server(
            Settings(database_path=":memory:", cursor_secret="contract-secret", sync_on_start=False),
            self.db,
        )

    def tearDown(self) -> None:
        self.db.close()

    def test_public_tool_schemas_use_documented_argument_names(self) -> None:
        async def run() -> dict[str, object]:
            tools = {tool.name: tool for tool in await self.server.list_tools()}
            return {
                "names": set(tools),
                "search": tools["icloud.search"].parameters["properties"],
                "mail_list": tools["icloud.mail.list"].parameters["properties"],
                "search_annotations": tools["icloud.search"].annotations,
            }

        result = asyncio.run(run())

        self.assertIn("icloud.search", result["names"])
        self.assertIn("icloud.mail.list", result["names"])
        self.assertIn("freshness", result["search"])
        self.assertNotIn("freshness_policy", result["search"])
        self.assertIn("from", result["mail_list"])
        self.assertNotIn("from_email", result["mail_list"])
        self.assertTrue(result["search_annotations"].readOnlyHint)
        self.assertTrue(result["search_annotations"].idempotentHint)
        self.assertFalse(result["search_annotations"].openWorldHint)

    def test_cursor_tools_return_deterministic_invalid_cursor_status(self) -> None:
        async def run() -> dict[str, object]:
            results = {}
            for tool_name in [
                "icloud.search",
                "icloud.mail.list",
                "icloud.contacts.list",
                "icloud.contacts.search",
                "icloud.calendar.list_events",
            ]:
                arguments = {"cursor": "bad-cursor"}
                if tool_name in {"icloud.search", "icloud.contacts.search"}:
                    arguments["query"] = "liesa"
                result = await self.server.call_tool(tool_name, arguments)
                results[tool_name] = result.structured_content
            return results

        results = asyncio.run(run())

        for result in results.values():
            self.assertEqual(result["status"], "invalid_cursor")
            self.assertEqual(result["reason"], "tampered_or_malformed")

    def test_search_default_domains_remain_all_public_domains(self) -> None:
        upsert_search_document(
            self.db,
            document_id="doc_mail",
            domain="mail",
            object_id="mail_1",
            title="Needle mail",
            text="email-only default-domain needle",
            metadata={"date": "2026-04-24T09:00:00+00:00"},
        )

        result = SearchService(self.db, Settings(database_path=":memory:", cursor_secret="contract-secret")).search(
            query="email-only default-domain needle",
            domains=None,
            start=None,
            end=None,
            person=None,
            limit=10,
            include_body_snippets=True,
            freshness_policy="allow_stale",
            cursor_payload={"offset": 0},
        )

        self.assertEqual(result["results"][0]["id"], "mail_1")


if __name__ == "__main__":
    unittest.main()
