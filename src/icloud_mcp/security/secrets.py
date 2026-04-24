"""Credential access boundary."""

from __future__ import annotations

from dataclasses import dataclass

from icloud_mcp.config import Settings


@dataclass(frozen=True)
class ICloudCredentials:
    """Out-of-band iCloud credentials."""

    apple_id: str
    app_password: str


def load_icloud_credentials(settings: Settings) -> ICloudCredentials | None:
    """Load credentials from environment-backed settings."""

    if not settings.apple_id or not settings.app_password:
        return None
    return ICloudCredentials(apple_id=settings.apple_id, app_password=settings.app_password)
