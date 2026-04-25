"""Mail repository interface."""

from __future__ import annotations

from icloud_mcp.db.repositories import list_mail as list_mail
from icloud_mcp.db.repositories import mailboxes_for_backfill as mailboxes_for_backfill
from icloud_mcp.db.repositories import tombstone_mail_message as tombstone_mail_message
from icloud_mcp.db.repositories import tombstone_mail_message_by_uid as tombstone_mail_message_by_uid
from icloud_mcp.db.repositories import update_mailbox_state as update_mailbox_state
from icloud_mcp.db.repositories import upsert_mail_message as upsert_mail_message
from icloud_mcp.db.repositories import upsert_mailbox as upsert_mailbox
from icloud_mcp.db.repositories import view_mail as view_mail

__all__ = [
    "list_mail",
    "mailboxes_for_backfill",
    "tombstone_mail_message",
    "tombstone_mail_message_by_uid",
    "update_mailbox_state",
    "upsert_mail_message",
    "upsert_mailbox",
    "view_mail",
]
