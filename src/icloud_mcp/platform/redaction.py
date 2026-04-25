"""Redaction helpers for logs and tool metadata."""

from __future__ import annotations

import re

EMAIL_PATTERN = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
APP_PASSWORD_PATTERN = re.compile(r"\b[a-zA-Z0-9]{4}(?:-[a-zA-Z0-9]{4}){3}\b")


def redact_email(value: str) -> str:
    """Redact email local parts while preserving domain context."""

    return EMAIL_PATTERN.sub(r"\1***\2", value)


def redact_secret(value: str | None) -> str | None:
    """Redact secret values completely."""

    if value is None:
        return None
    return "***"


def redact_text(value: str | None, *, allow_unredacted: bool = False) -> str | None:
    """Redact emails and app-specific password-shaped values from free text."""

    if value is None or allow_unredacted:
        return value
    return APP_PASSWORD_PATTERN.sub("***", redact_email(value))
