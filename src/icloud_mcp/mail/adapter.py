"""IMAP adapter for read-only iCloud Mail sync."""

from __future__ import annotations

import email
import hashlib
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from io import BytesIO
from typing import Any

from icalendar import Calendar

from icloud_mcp.platform.util import truncate
from icloud_mcp.search.normalizers import html_to_text


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
    last_synced_uid: int | None = None
    backfill_cursor: str | None = None
    backfill_status: str | None = None


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
    body_html: str = ""
    bcc_addresses: list[dict[str, str]] = field(default_factory=list)
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    calendar_invites: list[dict[str, Any]] = field(default_factory=list)
    body_unavailable_reason: str | None = None


@dataclass(frozen=True)
class DeletedMailMessage:
    """Mailbox UID deleted or moved remotely."""

    mailbox_id: str
    uid: int


@dataclass(frozen=True)
class IMAPSyncDelta:
    """Incremental IMAP sync result."""

    mailboxes: list[SyncedMailbox]
    messages: list[SyncedMailMessage]
    deleted: list[DeletedMailMessage]


class IMAPMailAdapter:
    """Read-only IMAP client for iCloud Mail."""

    def __init__(self, config: IMAPMailConfig | None = None) -> None:
        self.config = config or IMAPMailConfig()

    def sync_incremental(
        self,
        *,
        apple_id: str,
        app_password: str,
        mailbox_states: dict[str, dict[str, Any]],
        days: int,
        limit_per_mailbox: int,
    ) -> IMAPSyncDelta:
        """Fetch new/changed messages and detect missing known UIDs."""

        synced_mailboxes: list[SyncedMailbox] = []
        synced_messages: list[SyncedMailMessage] = []
        deleted: list[DeletedMailMessage] = []
        since = (datetime.now(tz=UTC) - timedelta(days=days)).date()

        with self._client() as client:
            client.login(apple_id, app_password)
            folder_names = self._folder_names(client.list_folders())
            for folder in folder_names:
                select_info = _select_folder_condstore(client, folder)
                mailbox_id = _mailbox_id(folder)
                state = mailbox_states.get(mailbox_id, {})
                uid_validity = _string_value(select_info.get(b"UIDVALIDITY"))
                highest_modseq = _string_value(select_info.get(b"HIGHESTMODSEQ"))
                if state.get("uid_validity") and state.get("uid_validity") != uid_validity:
                    uids = [int(uid) for uid in client.search(["SINCE", since])]
                    deleted.extend(
                        DeletedMailMessage(mailbox_id=mailbox_id, uid=uid) for uid in state.get("known_uids", [])
                    )
                else:
                    uids = _incremental_uids(client, state, since=since)
                    known_uids = {int(uid) for uid in state.get("known_uids", [])}
                    if known_uids:
                        remote_uids = {int(uid) for uid in client.search(["ALL"])}
                        deleted.extend(
                            DeletedMailMessage(mailbox_id=mailbox_id, uid=uid)
                            for uid in sorted(known_uids - remote_uids)
                        )
                if limit_per_mailbox > 0:
                    uids = sorted(set(uids))[-limit_per_mailbox:]
                synced_messages.extend(_fetch_messages(client, mailbox_id, uids))
                synced_mailboxes.append(
                    SyncedMailbox(
                        id=mailbox_id,
                        name=folder,
                        uid_validity=uid_validity,
                        uid_next=_int_value(select_info.get(b"UIDNEXT")),
                        highest_modseq=highest_modseq,
                        last_synced_uid=max(uids + [int(state.get("last_synced_uid") or 0)], default=None),
                        backfill_cursor=state.get("backfill_cursor"),
                        backfill_status=state.get("backfill_status"),
                    )
                )
        return IMAPSyncDelta(mailboxes=synced_mailboxes, messages=synced_messages, deleted=deleted)

    def sync_backfill(
        self,
        *,
        apple_id: str,
        app_password: str,
        mailbox: str,
        cursor: str | None,
        limit: int,
    ) -> tuple[SyncedMailbox, list[SyncedMailMessage]]:
        """Fetch one bounded batch of older messages for a mailbox."""

        with self._client() as client:
            client.login(apple_id, app_password)
            select_info = client.select_folder(mailbox, readonly=True)
            mailbox_id = _mailbox_id(mailbox)
            uids = _backfill_uids(client, cursor)
            batch = sorted(uids)[-limit:] if limit > 0 else []
            messages = _fetch_messages(client, mailbox_id, batch)
            oldest_uid = min(batch) if batch else None
            next_cursor = f"uid:{oldest_uid}" if oldest_uid and oldest_uid > 1 and len(batch) == limit else None
            return (
                SyncedMailbox(
                    id=mailbox_id,
                    name=mailbox,
                    uid_validity=_string_value(select_info.get(b"UIDVALIDITY")),
                    uid_next=_int_value(select_info.get(b"UIDNEXT")),
                    highest_modseq=_string_value(select_info.get(b"HIGHESTMODSEQ")),
                    last_synced_uid=max(batch, default=None),
                    backfill_cursor=next_cursor,
                    backfill_status="partial" if next_cursor else "complete",
                ),
                messages,
            )

    def _client(self) -> Any:
        from imapclient import IMAPClient

        return IMAPClient(host=self.config.host, port=self.config.port, ssl=self.config.ssl, use_uid=True)

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


def _fetch_messages(client: Any, mailbox_id: str, uids: list[int]) -> list[SyncedMailMessage]:
    if not uids:
        return []
    messages = []
    response = client.fetch(uids, ["BODY.PEEK[]", "FLAGS", "RFC822.SIZE", "INTERNALDATE"])
    for uid, data in response.items():
        raw = data.get(b"BODY.PEEK[]") or data.get(b"BODY[]") or data.get(b"RFC822")
        if not raw:
            continue
        messages.append(
            _message_from_email(
                mailbox_id=mailbox_id,
                uid=int(uid),
                message=email.message_from_bytes(raw),
                flags=data.get(b"FLAGS", ()),
                size_bytes=int(data.get(b"RFC822.SIZE", 0) or 0),
                internal_date=data.get(b"INTERNALDATE"),
            )
        )
    return messages


def _select_folder_condstore(client: Any, folder: str) -> dict[Any, Any]:
    try:
        return client.select_folder(folder, readonly=True, condstore=True)
    except TypeError:
        return client.select_folder(folder, readonly=True)


def _incremental_uids(client: Any, state: dict[str, Any], *, since: date) -> list[int]:
    if state.get("last_synced_uid") and not state.get("known_uids"):
        return [int(uid) for uid in client.search(["SINCE", since])]
    last_uid = int(state.get("last_synced_uid") or 0)
    uid_query = ["UID", f"{last_uid + 1}:*"] if last_uid else ["SINCE", since]
    uids = {int(uid) for uid in client.search(uid_query)}
    highest_modseq = state.get("highest_modseq")
    if highest_modseq:
        with suppress(Exception):
            uids.update(int(uid) for uid in client.search(["MODSEQ", str(highest_modseq)]))
    return sorted(uids)


def _backfill_uids(client: Any, cursor: str | None) -> list[int]:
    if cursor and cursor.startswith("uid:"):
        before_uid = max(1, int(cursor.removeprefix("uid:")) - 1)
        return [int(uid) for uid in client.search(["UID", f"1:{before_uid}"])]
    return [int(uid) for uid in client.search(["BEFORE", _cursor_before_date(cursor)])]


def _cursor_before_date(cursor: str | None) -> date:
    if cursor and ":" in cursor:
        raw_date = cursor.split(":", 1)[0]
        try:
            return date.fromisoformat(raw_date)
        except ValueError:
            pass
    return (datetime.now(tz=UTC) - timedelta(days=30)).date()


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
    unavailable_reason = _body_unavailable_reason(message)
    body_text, body_html = ("", "") if unavailable_reason else _body_parts(message)
    message_id = message.get("Message-ID")
    stable_id = _message_id(mailbox_id, uid, message_id)
    attachments = _attachments(message, stable_id)
    invites = _calendar_invites(message)
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
        body_html=body_html,
        has_attachments=bool(attachments),
        bcc_addresses=_addresses(message.get("Bcc", "")),
        in_reply_to=message.get("In-Reply-To"),
        references=_references(message.get("References", "")),
        attachments=attachments,
        calendar_invites=invites,
        body_unavailable_reason=unavailable_reason,
    )


def _body_text(message: Message) -> str:
    return _body_parts(message)[0]


def _body_parts(message: Message) -> tuple[str, str]:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disposition = _header_text(part.get("Content-Disposition")).lower()
            if "attachment" in disposition:
                continue
            _append_part_text(part, plain_parts, html_parts)
    else:
        _append_part_text(message, plain_parts, html_parts)

    html = "\n".join(html_parts).strip()
    text = "\n".join(plain_parts).strip() or "\n".join(html_to_text(part) for part in html_parts).strip()
    return "\n".join(line.rstrip() for line in text.splitlines() if line.strip()), html


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


def _attachments(message: Message, stable_message_id: str = "") -> list[dict[str, Any]]:
    attachments = []
    for index, part in enumerate(message.walk()):
        disposition = _header_text(part.get("Content-Disposition")).lower()
        filename = part.get_filename()
        if "attachment" not in disposition and not filename:
            continue
        payload = part.get_payload(decode=True) or b""
        content_id = _header_text(part.get("Content-ID")).strip("<>") or None
        filename_text = _decode_header(filename or "")
        text, unavailable_reason = _attachment_text(part.get_content_type(), payload)
        attachments.append(
            {
                "attachment_id": _attachment_id(stable_message_id, index, filename_text, content_id),
                "filename": filename_text,
                "mime_type": part.get_content_type(),
                "size_bytes": len(payload),
                "content_id": content_id,
                "disposition": disposition.split(";", 1)[0] or "attachment",
                "text": text,
                "text_indexed": bool(text),
                "text_unavailable_reason": unavailable_reason,
            }
        )
    return attachments


def _attachment_id(stable_message_id: str, index: int, filename: str, content_id: str | None) -> str:
    source = f"{stable_message_id}:{index}:{filename}:{content_id or ''}"
    return f"att_{hashlib.sha256(source.encode('utf-8')).hexdigest()[:16]}"


def _attachment_text(mime_type: str, payload: bytes) -> tuple[str, str | None]:
    if mime_type != "application/pdf":
        return "", None
    logger = logging.getLogger("pypdf")
    previous_level = logger.level
    logger.setLevel(logging.CRITICAL + 1)
    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(payload))
        text = "\n".join(page.extract_text() or "" for page in reader.pages).strip()
        return text, None if text else "pdf_text_empty"
    except Exception:
        return "", "pdf_extract_failed"
    finally:
        logger.setLevel(previous_level)


def _calendar_invites(message: Message) -> list[dict[str, Any]]:
    invites = []
    for part in message.walk():
        if part.get_content_type() != "text/calendar":
            continue
        raw = _part_text(part)
        if not raw:
            continue
        try:
            calendar = Calendar.from_ical(raw)
        except ValueError:
            continue
        method = str(calendar.get("METHOD", "")) or None
        for component in calendar.walk():
            if component.name != "VEVENT":
                continue
            attendees = [str(attendee).replace("mailto:", "") for attendee in _as_list(component.get("ATTENDEE"))]
            organizer = component.get("ORGANIZER")
            invites.append(
                {
                    "uid": str(component.get("UID", "")) or None,
                    "method": method,
                    "summary": str(component.get("SUMMARY", "")) or None,
                    "start": _date_value(getattr(component.get("DTSTART"), "dt", None)),
                    "end": _date_value(getattr(component.get("DTEND"), "dt", None)),
                    "timezone": _timezone_name(getattr(component.get("DTSTART"), "dt", None)),
                    "organizer": str(organizer).replace("mailto:", "") if organizer else None,
                    "attendees": attendees,
                }
            )
    return invites


def _part_text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def _body_unavailable_reason(message: Message) -> str | None:
    content_type = message.get_content_type()
    if content_type == "multipart/encrypted":
        return "encrypted"
    for part in message.walk():
        part_type = part.get_content_type()
        if part_type in {"application/pkcs7-mime", "application/x-pkcs7-mime", "application/pgp-encrypted"}:
            return "encrypted_or_signed"
    return None


def _decode_header(value: str) -> str:
    if not value:
        return ""
    return str(make_header(decode_header(value)))


def _header_text(value: Any) -> str:
    return str(value) if value is not None else ""


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


def _references(value: str) -> list[str]:
    return [part for part in value.split() if part]


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _date_value(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return None


def _timezone_name(value: Any) -> str | None:
    tzinfo = getattr(value, "tzinfo", None)
    return str(tzinfo) if tzinfo else None


def _mailbox_id(name: str) -> str:
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    return f"mb_{digest}"


def _message_id(mailbox_id: str, uid: int, message_id: str | None) -> str:
    source = f"{mailbox_id}:{uid}"
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
