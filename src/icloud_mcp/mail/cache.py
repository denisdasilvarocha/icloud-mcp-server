"""Mail repository interface."""

from __future__ import annotations

from typing import Any

from icloud_mcp.platform.util import compact_json, next_cursor, normalize_text, parse_json, sha256_text, utc_now
from icloud_mcp.search.repository import upsert_search_document
from icloud_mcp.storage.cache_state import bump_index_generation
from icloud_mcp.storage.connection import Database


def tombstone_mail_message(db: Database, message_id: str) -> None:
    """Mark a mail message deleted and cleanup search rows."""

    now = utc_now()
    db.execute("UPDATE mail_messages SET deleted_at = ? WHERE id = ?", (now, message_id))
    db.execute(
        "UPDATE search_documents SET deleted_at = ? WHERE object_id = ? AND domain IN ('mail','mail_invite')",
        (now, message_id),
    )
    db.execute("DELETE FROM search_fts WHERE object_id = ? AND domain IN ('mail','mail_invite')", (message_id,))
    bump_index_generation(db)


def tombstone_mail_message_by_uid(db: Database, mailbox_id: str, uid: int) -> None:
    """Mark a mail message deleted by IMAP mailbox UID."""

    row = db.query_one(
        "SELECT id FROM mail_messages WHERE mailbox_id = ? AND uid = ? AND deleted_at IS NULL",
        (mailbox_id, uid),
    )
    if row:
        tombstone_mail_message(db, row["id"])


def list_mail(
    db: Database,
    *,
    mailbox: str,
    after: str | None,
    before: str | None,
    sender: str | None,
    limit: int,
    offset: int,
    cursor_secret: str,
) -> dict[str, Any]:
    """List compact mail rows."""

    filters = ["m.deleted_at IS NULL", "mb.name = ?"]
    parameters: list[Any] = [mailbox]
    if after:
        filters.append("m.date >= ?")
        parameters.append(after)
    if before:
        filters.append("m.date <= ?")
        parameters.append(before)
    if sender:
        filters.append("m.from_json LIKE ?")
        parameters.append(f"%{sender}%")

    rows = db.query(
        f"""
        SELECT m.*, mb.name AS mailbox_name
        FROM mail_messages m
        JOIN mailboxes mb ON mb.id = m.mailbox_id
        WHERE {" AND ".join(filters)}
        ORDER BY m.date DESC
        LIMIT ? OFFSET ?
        """,
        (*parameters, limit + 1, offset),
    )
    has_more = len(rows) > limit
    messages = [
        {
            "id": row["id"],
            "mailbox": row["mailbox_name"],
            "subject": row["subject"],
            "from": parse_json(row["from_json"], {}),
            "date": row["date"],
            "preview": row["preview"],
            "has_attachments": bool(row["has_attachments"]),
        }
        for row in rows[:limit]
    ]
    return {
        "messages": messages,
        "next_cursor": next_cursor(offset, len(messages), limit, cursor_secret, has_more=has_more),
    }


def view_mail(
    db: Database,
    message_id: str,
    include: list[str],
    max_body_chars: int,
    body_offset: int = 0,
) -> dict[str, Any] | None:
    """Return one mail message with optional compact body."""

    row = db.query_one("SELECT * FROM mail_messages WHERE id = ? AND deleted_at IS NULL", (message_id,))
    if not row:
        return None
    result: dict[str, Any] = {
        "id": row["id"],
        "subject": row["subject"],
        "date": row["date"],
    }
    include_set = _mail_view_includes(include)
    if "headers" in include:
        result["headers"] = {
            "from": parse_json(row["from_json"], {}),
            "to": parse_json(row["to_json"], []),
            "cc": parse_json(row["cc_json"], []),
            "bcc": parse_json(row.get("bcc_json"), []),
            "message_id": row["message_id"],
            "in_reply_to": row.get("in_reply_to"),
            "references": parse_json(row.get("references_json"), []),
            "flags": parse_json(row["flags_json"], []),
        }
    if "body_text" in include_set:
        body = row["body_text"] or ""
        safe_offset = max(0, min(body_offset, len(body)))
        body_end = safe_offset + max_body_chars
        result["body_text"] = body[safe_offset:body_end]
        result["body_truncated"] = body_end < len(body)
        result["body_offset"] = safe_offset
        result["body_unavailable_reason"] = row.get("body_unavailable_reason") or ("empty" if not body else None)
        next_offset = body_end if body_end < len(body) else None
        result["next_body_offset"] = next_offset
        result["body_continuation"] = {
            "available": next_offset is not None,
            "offset": safe_offset,
            "next_offset": next_offset,
            "returned_chars": len(result["body_text"]),
            "total_chars": len(body),
            "indexed_chars": row.get("body_indexed_chars") or 0,
        }
    if "body_html" in include_set:
        result["body_html"] = row.get("body_html") or ""
    if "attachments" in include_set:
        result["attachments"] = parse_json(row.get("attachments_json"), [])
    result["content_trust"] = "untrusted_user_data"
    return result


def view_mail_attachment_text(
    db: Database,
    message_id: str,
    attachment_id: str,
    max_chars: int,
    offset: int = 0,
) -> dict[str, Any] | None:
    """Return one cached attachment text slice."""

    row = db.query_one(
        """
        SELECT *
        FROM mail_attachment_texts
        WHERE message_id = ? AND attachment_id = ?
        """,
        (message_id, attachment_id),
    )
    if not row:
        return None
    text = row["text"] or ""
    safe_offset = max(0, min(offset, len(text)))
    text_end = safe_offset + max_chars
    next_offset = text_end if text_end < len(text) else None
    return {
        "message_id": message_id,
        "attachment_id": attachment_id,
        "filename": row.get("filename"),
        "mime_type": row.get("mime_type"),
        "text": text[safe_offset:text_end],
        "offset": safe_offset,
        "text_truncated": next_offset is not None,
        "next_offset": next_offset,
        "text_unavailable_reason": row.get("text_unavailable_reason") if not text else None,
        "content_trust": "untrusted_user_data",
    }


def mailboxes_for_backfill(db: Database, limit: int) -> list[dict[str, Any]]:
    """Return mailboxes with older mail backfill still pending."""

    return db.query(
        """
        SELECT id, name, backfill_cursor, backfill_status
        FROM mailboxes
        WHERE COALESCE(backfill_status, 'not_started') != 'complete'
        ORDER BY last_sync_at DESC, name ASC
        LIMIT ?
        """,
        (limit,),
    )


def upsert_mailbox(
    db: Database, *, account_id: str, mailbox_id: str, name: str, last_sync_at: str | None = None
) -> None:
    """Upsert a mailbox discovered by IMAP sync."""

    db.execute(
        """
        INSERT INTO mailboxes (id, account_id, name, folder_quality, backfill_status, last_sync_at)
        VALUES (?, ?, ?, ?, 'not_started', ?)
        ON CONFLICT(account_id, name) DO UPDATE SET
          id = excluded.id,
          folder_quality = excluded.folder_quality,
          last_sync_at = COALESCE(excluded.last_sync_at, mailboxes.last_sync_at)
        """,
        (mailbox_id, account_id, name, _mailbox_quality(name), last_sync_at),
    )


def update_mailbox_state(
    db: Database,
    *,
    mailbox_id: str,
    uid_validity: str | None,
    uid_next: int | None,
    highest_modseq: str | None,
    last_synced_uid: int | None = None,
    backfill_cursor: str | None = None,
    backfill_status: str | None = None,
    last_sync_at: str | None = None,
) -> None:
    """Update IMAP mailbox sync metadata."""

    db.execute(
        """
        UPDATE mailboxes
        SET uid_validity = ?,
            uid_next = ?,
            highest_modseq = ?,
            last_synced_uid = COALESCE(?, last_synced_uid),
            backfill_cursor = COALESCE(?, backfill_cursor),
            backfill_status = COALESCE(?, backfill_status),
            last_sync_at = ?
        WHERE id = ?
        """,
        (
            uid_validity,
            uid_next,
            highest_modseq,
            last_synced_uid,
            backfill_cursor,
            backfill_status,
            last_sync_at or utc_now(),
            mailbox_id,
        ),
    )


def upsert_mail_message(
    db: Database,
    *,
    account_id: str,
    mailbox_id: str,
    message_id: str,
    uid: int,
    subject: str,
    from_address: dict[str, str],
    to_addresses: list[dict[str, str]],
    date: str,
    preview: str,
    body_text: str,
    body_html: str = "",
    cc_addresses: list[dict[str, str]] | None = None,
    bcc_addresses: list[dict[str, str]] | None = None,
    header_message_id: str | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    flags: list[str] | None = None,
    size_bytes: int | None = None,
    has_attachments: bool = False,
    attachments: list[dict[str, Any]] | None = None,
    calendar_invites: list[dict[str, Any]] | None = None,
    body_unavailable_reason: str | None = None,
    max_index_chars: int = 16000,
) -> None:
    """Upsert a synced mail message and index body text."""

    now = utc_now()
    attachment_rows = [_attachment_row(message_id, attachment) for attachment in attachments or []]
    attachment_metadata = [_attachment_metadata(row) for row in attachment_rows]
    has_attachment_value = has_attachments or bool(attachment_rows)
    indexed_body = _searchable_mail_body(body_text)[:max_index_chars]
    body_reason = body_unavailable_reason or ("empty" if not body_text else None)
    thread_id = _mail_thread_id(header_message_id or message_id, in_reply_to, references or [])
    db.execute(
        """
        INSERT INTO mail_messages
          (id, account_id, mailbox_id, uid, message_id, thread_id, subject, from_json, to_json, cc_json, bcc_json,
           in_reply_to, references_json, date, flags_json, size_bytes, preview, body_text, body_html, body_hash,
           body_unavailable_reason, body_indexed_chars, has_attachments, attachments_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(mailbox_id, uid) DO UPDATE SET
          id = excluded.id,
          message_id = excluded.message_id,
          thread_id = excluded.thread_id,
          subject = excluded.subject,
          from_json = excluded.from_json,
          to_json = excluded.to_json,
          cc_json = excluded.cc_json,
          bcc_json = excluded.bcc_json,
          in_reply_to = excluded.in_reply_to,
          references_json = excluded.references_json,
          date = excluded.date,
          flags_json = excluded.flags_json,
          size_bytes = excluded.size_bytes,
          preview = excluded.preview,
          body_text = excluded.body_text,
          body_html = excluded.body_html,
          body_hash = excluded.body_hash,
          body_unavailable_reason = excluded.body_unavailable_reason,
          body_indexed_chars = excluded.body_indexed_chars,
          has_attachments = excluded.has_attachments,
          attachments_json = excluded.attachments_json,
          updated_at = excluded.updated_at,
          deleted_at = NULL
        """,
        (
            message_id,
            account_id,
            mailbox_id,
            uid,
            header_message_id or message_id,
            thread_id,
            subject,
            compact_json(from_address),
            compact_json(to_addresses),
            compact_json(cc_addresses or []),
            compact_json(bcc_addresses or []),
            in_reply_to,
            compact_json(references or []),
            date,
            compact_json(flags or []),
            size_bytes,
            preview,
            body_text,
            body_html,
            sha256_text(body_text),
            body_reason,
            len(indexed_body),
            1 if has_attachment_value else 0,
            compact_json(attachment_metadata),
            now,
        ),
    )
    db.execute("DELETE FROM mail_attachment_texts WHERE message_id = ?", (message_id,))
    for attachment in attachment_rows:
        if attachment["text"] or attachment.get("text_unavailable_reason"):
            db.execute(
                """
                INSERT INTO mail_attachment_texts
                  (message_id, attachment_id, filename, mime_type, text, text_hash, text_unavailable_reason, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    attachment["attachment_id"],
                    attachment.get("filename"),
                    attachment.get("mime_type"),
                    attachment["text"],
                    sha256_text(attachment["text"]),
                    attachment.get("text_unavailable_reason"),
                    now,
                ),
            )
    sender = " ".join([from_address.get("name", ""), from_address.get("email", "")]).strip()
    recipients = " ".join(
        " ".join([address.get("name", ""), address.get("email", "")]).strip()
        for address in [*to_addresses, *(cc_addresses or []), *(bcc_addresses or [])]
    )
    mailbox = db.query_one("SELECT name, folder_quality FROM mailboxes WHERE id = ?", (mailbox_id,)) or {}
    metadata = {
        "date": date,
        "from": from_address,
        "to": to_addresses,
        "mailbox": mailbox.get("name"),
        "source_quality": mailbox.get("folder_quality") or "normal",
        "has_attachments": has_attachment_value,
        "attachments": attachment_metadata,
        "body_unavailable_reason": body_reason,
    }
    upsert_search_document(
        db,
        document_id=f"doc_{message_id}",
        domain="mail",
        object_id=message_id,
        title=subject,
        text="\n".join([f"Subject: {subject}", f"From: {sender}", f"Date: {date}", indexed_body]),
        metadata=metadata,
        sender=sender,
        participants=recipients,
        chunks=_mail_chunks(
            subject,
            from_address,
            to_addresses,
            cc_addresses or [],
            bcc_addresses or [],
            preview,
            indexed_body,
            attachment_rows,
        ),
    )
    db.execute(
        """
        UPDATE search_documents
        SET deleted_at = ?
        WHERE domain = 'mail_invite' AND object_id = ? AND deleted_at IS NULL
        """,
        (now, message_id),
    )
    for invite in calendar_invites or []:
        _index_mail_invite(db, message_id=message_id, subject=subject, sender=sender, invite=invite)


def _mailbox_quality(name: str) -> str:
    normalized = normalize_text(name)
    if any(part in normalized for part in ["spam", "junk", "trash", "deleted"]):
        return "spam"
    if any(part in normalized for part in ["newsletter", "promotions", "bulk"]):
        return "newsletter"
    return "normal"


def _mail_view_includes(include: list[str]) -> set[str]:
    include_set = set(include)
    if include_set & {"body", "text"}:
        include_set.add("body_text")
    if include_set & {"html"}:
        include_set.add("body_html")
    return include_set


def _attachment_row(message_id: str, attachment: dict[str, Any]) -> dict[str, Any]:
    attachment_id = str(attachment.get("attachment_id") or f"att_{sha256_text(compact_json(attachment))[:16]}")
    text = str(attachment.get("text") or "")
    return {
        "message_id": message_id,
        "attachment_id": attachment_id,
        "filename": attachment.get("filename"),
        "mime_type": attachment.get("mime_type"),
        "size_bytes": attachment.get("size_bytes"),
        "content_id": attachment.get("content_id"),
        "disposition": attachment.get("disposition"),
        "text": text,
        "text_indexed": bool(text),
        "text_unavailable_reason": None if text else attachment.get("text_unavailable_reason"),
    }


def _attachment_metadata(attachment: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in attachment.items() if key not in {"message_id", "text"}}


def _mail_thread_id(header_message_id: str, in_reply_to: str | None, references: list[str]) -> str:
    source = references[0] if references else in_reply_to or header_message_id
    return f"thread_{sha256_text(source)[:24]}"


def _searchable_mail_body(body_text: str) -> str:
    lines = []
    quote_started = False
    for line in body_text.splitlines():
        stripped = line.strip()
        lowered = stripped.casefold()
        if not stripped:
            continue
        if stripped.startswith(">") or (lowered.startswith("on ") and lowered.endswith("wrote:")):
            quote_started = True
            continue
        if lowered in {"original message", "forwarded message"} or lowered.startswith("from: "):
            quote_started = True
            continue
        if not quote_started:
            lines.append(stripped)
    return "\n".join(lines) or body_text


def _mail_chunks(
    subject: str,
    from_address: dict[str, str],
    to_addresses: list[dict[str, str]],
    cc_addresses: list[dict[str, str]],
    bcc_addresses: list[dict[str, str]],
    preview: str,
    body_text: str,
    attachments: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    header_text = "\n".join(
        [
            f"Subject: {subject}",
            f"From: {from_address.get('name', '')} {from_address.get('email', '')}",
            f"To: {_addresses_text(to_addresses)}",
            f"Cc: {_addresses_text(cc_addresses)}",
            f"Bcc: {_addresses_text(bcc_addresses)}",
            f"Preview: {preview}",
        ]
    )
    chunks = [{"type": "header", "text": header_text}]
    stripped_body = body_text.strip()
    chunks.extend(
        {"type": "body", "text": stripped_body[index : index + 4000]} for index in range(0, len(stripped_body), 4000)
    )
    for attachment in attachments or []:
        text = attachment.get("text", "").strip()
        chunks.extend(
            {
                "type": "attachment",
                "text": text[index : index + 4000],
                "metadata": _attachment_metadata(attachment),
            }
            for index in range(0, len(text), 4000)
        )
    return chunks


def _addresses_text(addresses: list[dict[str, str]]) -> str:
    return " ".join(" ".join([address.get("name", ""), address.get("email", "")]).strip() for address in addresses)


def _index_mail_invite(db: Database, *, message_id: str, subject: str, sender: str, invite: dict[str, Any]) -> None:
    title = invite.get("summary") or subject
    text = "\n".join(
        str(part)
        for part in [
            f"Invite: {title}",
            f"Method: {invite.get('method') or ''}",
            f"UID: {invite.get('uid') or ''}",
            f"Start: {invite.get('start') or ''}",
            f"End: {invite.get('end') or ''}",
            f"Organizer: {invite.get('organizer') or ''}",
            f"Attendees: {' '.join(invite.get('attendees') or [])}",
        ]
        if part
    )
    upsert_search_document(
        db,
        document_id=f"doc_{message_id}_invite_{sha256_text(text)[:12]}",
        domain="mail_invite",
        object_id=message_id,
        title=str(title),
        text=text,
        metadata={
            "source_mail_id": message_id,
            "invite": invite,
            "time": {"start": invite.get("start"), "end": invite.get("end"), "timezone": invite.get("timezone")},
        },
        sender=sender,
        participants=" ".join(invite.get("attendees") or []),
        chunks=[{"type": "invite", "text": text}],
    )
