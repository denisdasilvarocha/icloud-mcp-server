"""Mail sync worker."""

from __future__ import annotations

from dataclasses import dataclass

from icloud_mcp.adapters.imap_mail import IMAPMailAdapter
from icloud_mcp.config import Settings
from icloud_mcp.db.connection import Database
from icloud_mcp.db.repositories import update_mailbox_state, upsert_mail_message, upsert_mailbox
from icloud_mcp.sync.checkpoints import update_checkpoint
from icloud_mcp.util import utc_now


@dataclass
class MailSyncWorker:
    """Synchronize recent iCloud Mail via read-only IMAP."""

    db: Database
    settings: Settings
    adapter: IMAPMailAdapter | None = None

    name = "mail_sync_worker"

    def run_once(self) -> dict:
        """Run one recent-mail sync cycle."""

        if not self.settings.apple_id or not self.settings.app_password:
            result = {"status": "skipped", "reason": "credentials_missing"}
            update_checkpoint(self.db, self.name, "skipped", result)
            return result

        adapter = self.adapter or IMAPMailAdapter()
        mailboxes, messages = adapter.sync_recent(
            apple_id=self.settings.apple_id,
            app_password=self.settings.app_password,
            days=self.settings.mail_sync_days,
            limit_per_mailbox=self.settings.mail_sync_limit_per_mailbox,
        )
        now = utc_now()
        for mailbox in mailboxes:
            upsert_mailbox(
                self.db,
                account_id=self.settings.default_account_id,
                mailbox_id=mailbox.id,
                name=mailbox.name,
                last_sync_at=now,
            )
            update_mailbox_state(
                self.db,
                mailbox_id=mailbox.id,
                uid_validity=mailbox.uid_validity,
                uid_next=mailbox.uid_next,
                highest_modseq=mailbox.highest_modseq,
                last_sync_at=now,
            )
        for message in messages:
            upsert_mail_message(
                self.db,
                account_id=self.settings.default_account_id,
                mailbox_id=message.mailbox_id,
                message_id=message.id,
                uid=message.uid,
                subject=message.subject,
                from_address=message.from_address,
                to_addresses=message.to_addresses,
                cc_addresses=message.cc_addresses,
                date=message.date,
                preview=message.preview,
                body_text=message.body_text,
                flags=message.flags,
                size_bytes=message.size_bytes,
                has_attachments=message.has_attachments,
            )
        result = {"status": "ok", "mailboxes": len(mailboxes), "messages": len(messages)}
        update_checkpoint(self.db, self.name, "ok", result)
        return result
