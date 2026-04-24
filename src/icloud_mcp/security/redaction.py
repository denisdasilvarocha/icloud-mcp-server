"""Redaction helpers for logs and tool metadata."""

from __future__ import annotations

import re

EMAIL_PATTERN = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def redact_email(value: str) -> str:
    """Redact email local parts while preserving domain context."""

    return EMAIL_PATTERN.sub(r"\1***\2", value)


def redact_secret(value: str | None) -> str | None:
    """Redact secret values completely."""

    if value is None:
        return None
    return "***"
