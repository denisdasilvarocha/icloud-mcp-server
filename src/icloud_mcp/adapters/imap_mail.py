"""IMAP adapter for read-only iCloud Mail sync."""

from __future__ import annotations

import email
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

from icloud_mcp.indexing.normalizers import html_to_text
from icloud_mcp.util import truncate


@dataclass(frozen=True)
class IMAPMailConfig:
    """iCloud IMAP connection settings."""

    host: str = "imap.mail.me.com"
    port: int = 993
    ssl: bool = True


@dataclass(frozen=True)
class SyncedMailbox:
    """IMAP mailbox metadata."""

    id: str
    name: str
    uid_validity: str | None
    uid_next: int | None
    highest_modseq: str | None


@dataclass(frozen=True)
class SyncedMailMessage:
    """Fetched mail message normalized for local storage."""

    id: str
    mailbox_id: str
    uid: int
    message_id: str | None
    subject: str
    from_address: dict[str, str]
    to_addresses: list[dict[str, str]]
    cc_addresses: list[dict[str, str]]
    date: str
    flags: list[str]
    size_bytes: int
    preview: str
    body_text: str
    has_attachments: bool


class IMAPMailAdapter:
    """Read-only IMAP client for iCloud Mail."""

    def __init__(self, config: IMAPMailConfig | None = None) -> None:
        self.config = config or IMAPMailConfig()

    def configured(self, apple_id: str | None, app_password: str | None) -> bool:
        """Return whether credentials are available out-of-band."""

        return bool(apple_id and app_password)

    def sync_recent(
        self,
        *,
        apple_id: str,
        app_password: str,
        days: int,
        limit_per_mailbox: int,
        mailboxes: list[str] | None = None,
    ) -> tuple[list[SyncedMailbox], list[SyncedMailMessage]]:
        """Fetch recent messages from iCloud IMAP without mutating server state."""

        from imapclient import IMAPClient

        synced_mailboxes: list[SyncedMailbox] = []
        synced_messages: list[SyncedMailMessage] = []
        since = (datetime.now(tz=UTC) - timedelta(days=days)).date()

        with IMAPClient(host=self.config.host, port=self.config.port, ssl=self.config.ssl, use_uid=True) as client:
            client.login(apple_id, app_password)
            folder_names = mailboxes or self._folder_names(client.list_folders())
            for folder in folder_names:
                select_info = client.select_folder(folder, readonly=True)
                mailbox_id = _mailbox_id(folder)
                synced_mailboxes.append(
                    SyncedMailbox(
                        id=mailbox_id,
                        name=folder,
                        uid_validity=_string_value(select_info.get(b"UIDVALIDITY")),
                        uid_next=_int_value(select_info.get(b"UIDNEXT")),
                        highest_modseq=_string_value(select_info.get(b"HIGHESTMODSEQ")),
                    )
                )
                uids = client.search(["SINCE", since])
                if limit_per_mailbox > 0:
                    uids = sorted(uids)[-limit_per_mailbox:]
                if not uids:
                    continue
                response = client.fetch(uids, ["RFC822", "FLAGS", "RFC822.SIZE", "INTERNALDATE"])
                for uid, data in response.items():
                    raw = data.get(b"RFC822")
                    if not raw:
                        continue
                    parsed = email.message_from_bytes(raw)
                    synced_messages.append(
                        _message_from_email(
                            mailbox_id=mailbox_id,
                            uid=int(uid),
                            message=parsed,
                            flags=data.get(b"FLAGS", ()),
                            size_bytes=int(data.get(b"RFC822.SIZE", 0) or 0),
                            internal_date=data.get(b"INTERNALDATE"),
                        )
                    )
        return synced_mailboxes, synced_messages

    def _folder_names(self, folders: list[tuple[Any, Any, str]]) -> list[str]:
        """Return folders worth syncing, with noisy folders delayed by ranking."""

        names = [folder[-1] for folder in folders]
        preferred = [name for name in names if name.upper() == "INBOX"]
        rest = [
            name
            for name in names
            if name not in preferred and not any(part in name.lower() for part in ["trash", "deleted", "junk", "spam"])
        ]
        return preferred + rest


def _message_from_email(
    *,
    mailbox_id: str,
    uid: int,
    message: Message,
    flags: tuple[bytes, ...],
    size_bytes: int,
    internal_date: Any,
) -> SyncedMailMessage:
    subject = _decode_header(message.get("Subject", ""))
    body_text = _body_text(message)
    message_id = message.get("Message-ID")
    stable_id = _message_id(mailbox_id, uid, message_id)
    return SyncedMailMessage(
        id=stable_id,
        mailbox_id=mailbox_id,
        uid=uid,
        message_id=message_id,
        subject=subject,
        from_address=_first_address(message.get("From", "")),
        to_addresses=_addresses(message.get("To", "")),
        cc_addresses=_addresses(message.get("Cc", "")),
        date=_message_date(message, internal_date),
        flags=[flag.decode("utf-8", errors="replace") if isinstance(flag, bytes) else str(flag) for flag in flags],
        size_bytes=size_bytes,
        preview=truncate(body_text, 240),
        body_text=body_text,
        has_attachments=_has_attachments(message),
    )


def _body_text(message: Message) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disposition = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            _append_part_text(part, plain_parts, html_parts)
    else:
        _append_part_text(message, plain_parts, html_parts)

    text = "\n".join(plain_parts).strip() or "\n".join(html_to_text(part) for part in html_parts).strip()
    return "\n".join(line.rstrip() for line in text.splitlines() if line.strip())


def _append_part_text(part: Message, plain_parts: list[str], html_parts: list[str]) -> None:
    content_type = part.get_content_type()
    if content_type not in {"text/plain", "text/html", "text/calendar"}:
        return
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        text = raw if isinstance(raw, str) else ""
    else:
        charset = part.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="replace")
    if content_type == "text/html":
        html_parts.append(text)
    else:
        plain_parts.append(text)


def _decode_header(value: str) -> str:
    if not value:
        return ""
    return str(make_header(decode_header(value)))


def _addresses(value: str) -> list[dict[str, str]]:
    return [{"name": _decode_header(name), "email": address} for name, address in getaddresses([value]) if address]


def _first_address(value: str) -> dict[str, str]:
    addresses = _addresses(value)
    return addresses[0] if addresses else {"name": "", "email": ""}


def _message_date(message: Message, internal_date: Any) -> str:
    raw_date = message.get("Date")
    if raw_date:
        try:
            return parsedate_to_datetime(raw_date).isoformat()
        except (TypeError, ValueError):
            pass
    if isinstance(internal_date, datetime):
        return internal_date.isoformat()
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat()


def _has_attachments(message: Message) -> bool:
    return any((part.get("Content-Disposition") or "").lower().startswith("attachment") for part in message.walk())


def _mailbox_id(name: str) -> str:
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    return f"mb_{digest}"


def _message_id(mailbox_id: str, uid: int, message_id: str | None) -> str:
    source = message_id or f"{mailbox_id}:{uid}"
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]
    return f"mail_msg_{digest}"


def _string_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _int_value(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
