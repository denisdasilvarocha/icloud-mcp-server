"""Mail sync worker."""

from __future__ import annotations

from dataclasses import dataclass

from icloud_mcp.mail.adapter import IMAPMailAdapter
from icloud_mcp.mail.cache import (
    mailboxes_for_backfill,
    tombstone_mail_message_by_uid,
    update_mailbox_state,
    upsert_mail_message,
    upsert_mailbox,
)
from icloud_mcp.platform.config import Settings
from icloud_mcp.platform.secrets import load_icloud_credentials
from icloud_mcp.platform.util import utc_now
from icloud_mcp.storage.connection import Database
from icloud_mcp.sync.checkpoints import update_checkpoint, update_failure_checkpoint


@dataclass
class MailSyncWorker:
    """Synchronize recent iCloud Mail via read-only IMAP."""

    db: Database
    settings: Settings
    adapter: IMAPMailAdapter | None = None

    name = "mail_sync_worker"

    def run_once(self) -> dict:
        """Run one recent-mail sync cycle."""

        credentials = load_icloud_credentials(self.settings)
        if not credentials:
            result = {"status": "skipped", "reason": "credentials_missing"}
            update_checkpoint(self.db, self.name, "skipped", result)
            return result

        try:
            adapter = self.adapter or IMAPMailAdapter()
            delta = adapter.sync_incremental(
                apple_id=credentials.apple_id,
                app_password=credentials.app_password,
                mailbox_states=_mailbox_states(self.db),
                days=self.settings.mail_sync_days,
                limit_per_mailbox=self.settings.mail_sync_limit_per_mailbox,
            )
            mailboxes, messages = delta.mailboxes, delta.messages
            for deleted in delta.deleted:
                tombstone_mail_message_by_uid(self.db, deleted.mailbox_id, deleted.uid)
            now = utc_now()
            for mailbox in mailboxes:
                backfill_cursor, backfill_status = _recent_sync_backfill_state(
                    self.db,
                    mailbox_id=mailbox.id,
                    cursor=mailbox.backfill_cursor,
                    status=mailbox.backfill_status,
                )
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
                    last_synced_uid=mailbox.last_synced_uid,
                    backfill_cursor=backfill_cursor,
                    backfill_status=backfill_status,
                    last_sync_at=now,
                )
            _upsert_messages(self.db, self.settings, messages)
            result = {
                "status": "ok",
                "mailboxes": len(mailboxes),
                "messages": len(messages),
                "last_synced_uid": max((message.uid for message in messages), default=None),
            }
            update_checkpoint(self.db, self.name, "ok", result)
            return result
        except Exception as exc:
            return update_failure_checkpoint(
                self.db,
                self.name,
                exc,
                allow_unredacted=self.settings.allow_unredacted_debug,
            )


@dataclass
class MailBackfillWorker:
    """Synchronize older iCloud Mail bodies in bounded checkpointed batches."""

    db: Database
    settings: Settings
    adapter: IMAPMailAdapter | None = None

    name = "mail_backfill_worker"

    def run_once(self) -> dict:
        """Run one older-mail backfill batch."""

        credentials = load_icloud_credentials(self.settings)
        if not credentials:
            result = {"status": "skipped", "reason": "credentials_missing"}
            update_checkpoint(self.db, self.name, "skipped", result)
            return result

        adapter = self.adapter or IMAPMailAdapter()
        sync_backfill = getattr(adapter, "sync_backfill", None)
        if sync_backfill is None:
            result = {"status": "skipped", "reason": "adapter_backfill_unsupported"}
            update_checkpoint(self.db, self.name, "skipped", result)
            return result

        try:
            candidates = mailboxes_for_backfill(self.db, limit=1)
            if not candidates:
                result = {"status": "complete", "mailboxes": 0, "messages": 0}
                update_checkpoint(self.db, self.name, "ok", result)
                return result

            candidate = candidates[0]
            mailbox, messages = sync_backfill(
                apple_id=credentials.apple_id,
                app_password=credentials.app_password,
                mailbox=candidate["name"],
                cursor=candidate.get("backfill_cursor"),
                limit=self.settings.mail_sync_limit_per_mailbox,
            )
            now = utc_now()
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
                last_synced_uid=mailbox.last_synced_uid,
                backfill_cursor=mailbox.backfill_cursor,
                backfill_status=mailbox.backfill_status,
                last_sync_at=now,
            )
            _upsert_messages(self.db, self.settings, messages)
            result = {
                "status": "ok",
                "mailbox": mailbox.name,
                "mailboxes": 1,
                "messages": len(messages),
                "backfill_cursor": mailbox.backfill_cursor,
                "backfill_status": mailbox.backfill_status,
            }
            update_checkpoint(self.db, self.name, "ok", result)
            return result
        except Exception as exc:
            return update_failure_checkpoint(
                self.db,
                self.name,
                exc,
                allow_unredacted=self.settings.allow_unredacted_debug,
            )


def _upsert_messages(db: Database, settings: Settings, messages: list) -> None:
    for message in messages:
        upsert_mail_message(
            db,
            account_id=settings.default_account_id,
            mailbox_id=message.mailbox_id,
            message_id=message.id,
            uid=message.uid,
            subject=message.subject,
            from_address=message.from_address,
            to_addresses=message.to_addresses,
            cc_addresses=message.cc_addresses,
            bcc_addresses=message.bcc_addresses,
            header_message_id=message.message_id,
            in_reply_to=message.in_reply_to,
            references=message.references,
            date=message.date,
            preview=message.preview,
            body_text=message.body_text,
            flags=message.flags,
            size_bytes=message.size_bytes,
            has_attachments=message.has_attachments,
            attachments=message.attachments,
            calendar_invites=message.calendar_invites,
            body_unavailable_reason=message.body_unavailable_reason,
            max_index_chars=settings.mail_index_body_chars,
        )


def _mailbox_states(db: Database) -> dict[str, dict]:
    rows = db.query(
        """
        SELECT mb.id, mb.uid_validity, mb.highest_modseq, mb.last_synced_uid,
               mb.backfill_cursor, mb.backfill_status, m.uid
        FROM mailboxes mb
        LEFT JOIN mail_messages m ON m.mailbox_id = mb.id AND m.deleted_at IS NULL
        ORDER BY mb.id
        """
    )
    states: dict[str, dict] = {}
    for row in rows:
        state = states.setdefault(
            row["id"],
            {
                "uid_validity": row.get("uid_validity"),
                "highest_modseq": row.get("highest_modseq"),
                "last_synced_uid": row.get("last_synced_uid"),
                "backfill_cursor": row.get("backfill_cursor"),
                "backfill_status": row.get("backfill_status"),
                "known_uids": [],
            },
        )
        if row.get("uid") is not None:
            state["known_uids"].append(int(row["uid"]))
    return states


def _recent_sync_backfill_state(
    db: Database,
    *,
    mailbox_id: str,
    cursor: str | None,
    status: str | None,
) -> tuple[str | None, str | None]:
    row = db.query_one("SELECT backfill_cursor, backfill_status FROM mailboxes WHERE id = ?", (mailbox_id,))
    if row and row.get("backfill_status") in {"partial", "complete"}:
        return row.get("backfill_cursor"), row.get("backfill_status")
    return cursor, status
