from __future__ import annotations

import asyncio
import unittest

from icloud_mcp.mail.cache import upsert_mail_message, upsert_mailbox
from icloud_mcp.mcp.server import create_server
from icloud_mcp.platform.config import Settings
from icloud_mcp.search.repository import upsert_search_document
from icloud_mcp.search.service import SearchService
from icloud_mcp.storage.connection import open_db


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

    def test_calendar_write_tool_schemas_keep_nested_input_contract(self) -> None:
        async def run() -> dict[str, object]:
            tools = {tool.name: tool for tool in await self.server.list_tools()}
            return {
                "create": tools["icloud.calendar.create_event"].parameters,
                "update": tools["icloud.calendar.update_event"].parameters,
                "create_annotations": tools["icloud.calendar.create_event"].annotations,
                "update_annotations": tools["icloud.calendar.update_event"].annotations,
            }

        result = asyncio.run(run())
        create = result["create"]
        update = result["update"]
        create_input = create["properties"]["input"]
        update_input = update["properties"]["input"]

        self.assertEqual(create["required"], ["input"])
        self.assertFalse(create["additionalProperties"])
        self.assertEqual(create_input["required"], ["title", "start", "end", "timezone"])
        self.assertEqual(create_input["properties"]["title"]["minLength"], 1)
        self.assertEqual(create_input["properties"]["title"]["maxLength"], 200)
        self.assertEqual(create_input["properties"]["attendees"]["items"]["additionalProperties"]["type"], "string")
        self.assertEqual(create_input["properties"]["alarms"]["items"]["type"], "object")
        self.assertEqual(create_input["properties"]["recurrence"]["default"], None)
        self.assertEqual(create_input["properties"]["calendar_id"]["default"], None)
        self.assertEqual(create_input["properties"]["request_id"]["default"], None)

        self.assertEqual(update["required"], ["input"])
        self.assertFalse(update["additionalProperties"])
        self.assertEqual(update_input["required"], ["event_id", "patch"])
        self.assertEqual(update_input["properties"]["patch"]["type"], "object")
        self.assertTrue(update_input["properties"]["patch"]["additionalProperties"])
        self.assertEqual(update_input["properties"]["scope"]["default"], "series")
        self.assertEqual(update_input["properties"]["etag"]["default"], None)

        for tool_annotations in [result["create_annotations"], result["update_annotations"]]:
            self.assertFalse(tool_annotations.readOnlyHint)
            self.assertFalse(tool_annotations.destructiveHint)
            self.assertFalse(tool_annotations.idempotentHint)
            self.assertTrue(tool_annotations.openWorldHint)

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

    def test_mail_attachment_text_tool_pages_cached_text(self) -> None:
        upsert_mailbox(self.db, account_id="local", mailbox_id="mb_inbox", name="INBOX")
        upsert_mail_message(
            self.db,
            account_id="local",
            mailbox_id="mb_inbox",
            message_id="mail_msg_pdf",
            uid=1,
            subject="Receipt",
            from_address={"name": "Shop", "email": "shop@example.com"},
            to_addresses=[{"name": "Me", "email": "me@example.com"}],
            date="2026-04-24T09:00:00+02:00",
            preview="Receipt",
            body_text="Body",
            attachments=[
                {
                    "attachment_id": "att_receipt",
                    "filename": "receipt.pdf",
                    "mime_type": "application/pdf",
                    "size_bytes": 100,
                    "text": "receipt text body",
                }
            ],
            has_attachments=True,
        )

        async def run() -> tuple[dict[str, object], dict[str, object]]:
            page = await self.server.call_tool(
                "icloud.mail.view_attachment_text",
                {"message_id": "mail_msg_pdf", "attachment_id": "att_receipt", "max_chars": 7, "offset": 8},
            )
            missing = await self.server.call_tool(
                "icloud.mail.view_attachment_text",
                {"message_id": "mail_msg_pdf", "attachment_id": "missing"},
            )
            return page.structured_content, missing.structured_content

        page, missing = asyncio.run(run())

        self.assertEqual(page["text"], "text bo")
        self.assertEqual(page["next_offset"], 15)
        self.assertEqual(missing["status"], "not_found")

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

    def test_search_defaults_use_planner_domains_for_invites(self) -> None:
        upsert_search_document(
            self.db,
            document_id="doc_invite",
            domain="mail_invite",
            object_id="mail_2",
            title="Party invite",
            text="party invite from Liesa",
            metadata={"date": "2026-04-24T09:00:00+00:00"},
        )

        result = SearchService(self.db, Settings(database_path=":memory:", cursor_secret="contract-secret")).search(
            query="party invite",
            domains=None,
            start=None,
            end=None,
            person=None,
            limit=10,
            include_body_snippets=True,
            freshness_policy="allow_stale",
            cursor_payload={"offset": 0},
        )

        self.assertEqual(result["results"][0]["domain"], "mail_invite")
        self.assertEqual(result["results"][0]["id"], "mail_2")

    def test_search_person_filter_stops_at_topic_delimiter(self) -> None:
        upsert_search_document(
            self.db,
            document_id="doc_contract",
            domain="mail",
            object_id="mail_3",
            title="Contract update",
            text="Contract update from Liesa",
            metadata={"from": {"name": "Liesa", "email": "liesa@example.com"}, "date": "2026-04-24T09:00:00+00:00"},
        )

        result = SearchService(self.db, Settings(database_path=":memory:", cursor_secret="contract-secret")).search(
            query="mail from Liesa about contract",
            domains=None,
            start=None,
            end=None,
            person=None,
            limit=10,
            include_body_snippets=True,
            freshness_policy="allow_stale",
            cursor_payload={"offset": 0},
        )

        self.assertEqual(result["results"][0]["id"], "mail_3")


if __name__ == "__main__":
    unittest.main()
