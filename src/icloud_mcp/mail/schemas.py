"""Mail schemas."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MailAddress:
    """Mail address."""

    name: str | None
    email: str
