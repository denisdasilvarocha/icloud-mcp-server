"""Credential access boundary."""

from __future__ import annotations

from dataclasses import dataclass

from icloud_mcp.config import Settings

KEYCHAIN_SERVICE = "icloud-mcp-server"
KEYCHAIN_APP_PASSWORD_USER = "app-password"


@dataclass(frozen=True)
class ICloudCredentials:
    """Out-of-band iCloud credentials."""

    apple_id: str
    app_password: str


def load_icloud_credentials(settings: Settings) -> ICloudCredentials | None:
    """Load credentials from environment settings, then OS keychain fallback."""

    apple_id = settings.apple_id
    app_password = settings.app_password
    if settings.use_keychain and apple_id and not app_password:
        app_password = _read_keychain_password(apple_id) or _read_keychain_password(KEYCHAIN_APP_PASSWORD_USER)
    if not apple_id or not app_password:
        return None
    return ICloudCredentials(apple_id=apple_id, app_password=app_password)


def store_icloud_credentials(apple_id: str, app_password: str) -> None:
    """Store an iCloud app-specific password in the OS keychain."""

    import keyring

    keyring.set_password(KEYCHAIN_SERVICE, apple_id, app_password)


def _read_keychain_password(account: str) -> str | None:
    try:
        import keyring

        return keyring.get_password(KEYCHAIN_SERVICE, account)
    except Exception:
        return None
