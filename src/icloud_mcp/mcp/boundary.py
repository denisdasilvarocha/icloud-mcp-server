"""Shared MCP tool boundary helpers."""

from __future__ import annotations

from typing import Any

from icloud_mcp.platform.util import cursor_error, decode_cursor


def bounded_int(value: int, *, minimum: int, maximum: int) -> int:
    """Clamp integer tool input to a deterministic public range."""

    return max(minimum, min(value, maximum))


def minimum_int(value: int, minimum: int) -> int:
    """Raise integer tool input to a deterministic public minimum."""

    return max(minimum, value)


def cursor_offset(payload: dict[str, Any] | None) -> int:
    """Return the cursor offset used by list/search tool calls."""

    return int((payload or {}).get("offset", 0))


def cursor_state_or_error(cursor: str | None, secret: str) -> tuple[dict[str, Any], dict[str, str] | None]:
    """Return a decoded cursor state with a stable offset default."""

    try:
        return decode_cursor(cursor, secret), None
    except ValueError as exc:
        return {"offset": 0}, cursor_error(exc)


def not_found(identifier: str, value: str) -> dict[str, str]:
    """Return a deterministic not-found envelope."""

    return {"status": "not_found", identifier: value}
