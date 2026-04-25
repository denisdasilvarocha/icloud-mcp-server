"""Contact schemas."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContactSummary:
    """Compact contact row."""

    id: str
    display_name: str
    emails: list[str]
