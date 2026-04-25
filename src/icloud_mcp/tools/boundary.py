"""Shared MCP tool boundary helpers."""

from __future__ import annotations

from typing import Any

from icloud_mcp.util import cursor_error, decode_cursor


def bounded_int(value: int, *, minimum: int, maximum: int) -> int:
    """Clamp integer tool input to a deterministic public range."""

    return max(minimum, min(value, maximum))


def minimum_int(value: int, minimum: int) -> int:
    """Raise integer tool input to a deterministic public minimum."""

    return max(minimum, value)


def decode_cursor_or_error(cursor: str | None, secret: str) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    """Decode a tool cursor, returning a structured error instead of raising."""

    try:
        return decode_cursor(cursor, secret), None
    except ValueError as exc:
        return None, cursor_error(exc)


def cursor_offset(payload: dict[str, Any] | None) -> int:
    """Return the cursor offset used by list/search tool calls."""

    return int((payload or {}).get("offset", 0))


def not_found(identifier: str, value: str) -> dict[str, str]:
    """Return a deterministic not-found envelope."""

    return {"status": "not_found", identifier: value}
