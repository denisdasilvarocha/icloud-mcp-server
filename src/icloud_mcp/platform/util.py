"""Shared utility helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

TOKEN_PATTERN = re.compile(r"[\w@.+-]+", re.UNICODE)


def utc_now() -> str:
    """Return current UTC time in ISO 8601 form."""

    return datetime.now(tz=UTC).replace(microsecond=0).isoformat()


def normalize_text(value: str) -> str:
    """Normalize text for search and alias matching."""

    return " ".join(value.casefold().strip().split())


def tokenize(value: str) -> list[str]:
    """Tokenize search input into simple safe terms."""

    return [match.group(0).casefold() for match in TOKEN_PATTERN.finditer(value) if match.group(0).strip()]


def sha256_text(value: str) -> str:
    """Hash text content."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def compact_json(value: Any) -> str:
    """Encode JSON with stable compact separators."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_json(value: str | None, default: Any) -> Any:
    """Decode JSON with fallback for nullable columns."""

    if not value:
        return default
    return json.loads(value)


def truncate(value: str | None, max_chars: int) -> str:
    """Return a compact string with ellipsis when truncated."""

    if not value:
        return ""
    normalized = " ".join(value.split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 1].rstrip() + "…"


def encode_cursor(payload: dict[str, Any], secret: str) -> str:
    """Encode and sign a pagination cursor."""

    body = compact_json(payload).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(body + b"." + base64.urlsafe_b64encode(signature)).decode("ascii")


def decode_cursor(cursor: str | None, secret: str) -> dict[str, Any]:
    """Decode and verify a cursor. Returns offset zero for empty cursors."""

    if not cursor:
        return {"offset": 0}
    decoded = base64.urlsafe_b64decode(cursor.encode("ascii"))
    body, signature_b64 = decoded.rsplit(b".", 1)
    expected = base64.urlsafe_b64encode(hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest())
    if not hmac.compare_digest(signature_b64, expected):
        raise ValueError("Invalid cursor signature")
    payload = json.loads(body.decode("utf-8"))
    expires_at = payload.get("expires_at")
    if expires_at and datetime.fromisoformat(expires_at) < datetime.now(tz=UTC):
        raise ValueError("Cursor expired")
    return payload


def cursor_error(exc: Exception) -> dict[str, str]:
    """Return deterministic public cursor error details."""

    message = str(exc).casefold()
    if "expired" in message:
        return {"status": "invalid_cursor", "reason": "expired"}
    return {"status": "invalid_cursor", "reason": "tampered_or_malformed"}


def next_cursor(
    offset: int,
    returned: int,
    limit: int,
    secret: str,
    extra: dict[str, Any] | None = None,
    *,
    has_more: bool | None = None,
) -> str | None:
    """Build next cursor when more rows may be available."""

    if has_more is None:
        has_more = returned >= limit
    if not has_more:
        return None
    payload = {
        "offset": offset + returned,
        "expires_at": (datetime.now(tz=UTC) + timedelta(hours=1)).replace(microsecond=0).isoformat(),
    }
    if extra:
        payload.update(extra)
    return encode_cursor(payload, secret)
