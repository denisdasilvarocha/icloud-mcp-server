"""Configuration loaded outside the MCP tool surface."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DATABASE_PATH = Path("~/.local/share/icloud-mcp/icloud-mcp.sqlite3").expanduser()


@dataclass(frozen=True)
class Settings:
    """Runtime settings for local STDIO deployment."""

    database_path: Path = DEFAULT_DATABASE_PATH
    cursor_secret: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    apple_id: str | None = None
    app_password: str | None = None
    default_account_id: str = "local"
    default_calendar_id: str = "cal_primary"
    default_addressbook_id: str = "addr_default"
    mail_body_view_chars: int = 8000
    mail_index_body_chars: int = 16000
    snippet_chars: int = 360
    query_cache_ttl_seconds: int = 300
    attachment_text_indexing: bool = False
    sync_on_start: bool = True
    sync_interval_seconds: int = 900
    stale_after_seconds: int = 86400
    mail_sync_days: int = 30
    mail_sync_limit_per_mailbox: int = 250
    calendar_past_months: int = 24
    calendar_future_months: int = 36
    use_keychain: bool = True
    allow_unredacted_debug: bool = False
    dashboard_host: str = "127.0.0.1"
    dashboard_public_host: str = "127.0.0.1"
    dashboard_port: int = 8765
    dashboard_allow_external_bind: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from environment variables."""

        database_path = Path(os.getenv("ICLOUD_MCP_DATABASE_PATH", str(DEFAULT_DATABASE_PATH))).expanduser()
        return cls(
            database_path=database_path,
            cursor_secret=_env_secret("ICLOUD_MCP_CURSOR_SECRET"),
            apple_id=os.getenv("ICLOUD_APPLE_ID"),
            app_password=os.getenv("ICLOUD_APP_PASSWORD"),
            mail_index_body_chars=_env_int("ICLOUD_MCP_MAIL_INDEX_BODY_CHARS", 16000, minimum=0, maximum=250000),
            query_cache_ttl_seconds=min(1800, max(300, _env_int("ICLOUD_MCP_QUERY_CACHE_TTL_SECONDS", 300, minimum=1))),
            sync_on_start=_env_bool("ICLOUD_MCP_SYNC_ON_START", True),
            sync_interval_seconds=_env_int("ICLOUD_MCP_SYNC_INTERVAL_SECONDS", 900, minimum=60),
            stale_after_seconds=_env_int("ICLOUD_MCP_STALE_AFTER_SECONDS", 86400, minimum=0),
            mail_sync_days=_env_int("ICLOUD_MCP_MAIL_SYNC_DAYS", 30, minimum=1, maximum=3650),
            mail_sync_limit_per_mailbox=_env_int(
                "ICLOUD_MCP_MAIL_SYNC_LIMIT_PER_MAILBOX", 250, minimum=1, maximum=5000
            ),
            calendar_past_months=_env_int("ICLOUD_MCP_CALENDAR_PAST_MONTHS", 24, minimum=0, maximum=240),
            calendar_future_months=_env_int("ICLOUD_MCP_CALENDAR_FUTURE_MONTHS", 36, minimum=0, maximum=240),
            use_keychain=_env_bool("ICLOUD_MCP_USE_KEYCHAIN", True),
            attachment_text_indexing=_env_bool("ICLOUD_MCP_ATTACHMENT_TEXT_INDEXING", False),
            allow_unredacted_debug=_env_bool("ICLOUD_MCP_ALLOW_UNREDACTED_DEBUG", False),
            dashboard_host=os.getenv("ICLOUD_MCP_DASHBOARD_HOST", "127.0.0.1"),
            dashboard_public_host=os.getenv("ICLOUD_MCP_DASHBOARD_PUBLIC_HOST", "127.0.0.1"),
            dashboard_port=_env_int("ICLOUD_MCP_DASHBOARD_PORT", 8765, minimum=1, maximum=65535),
            dashboard_allow_external_bind=_env_bool("ICLOUD_MCP_DASHBOARD_ALLOW_EXTERNAL_BIND", False),
        )


def _env_secret(name: str) -> str:
    value = os.getenv(name)
    if value and value.strip():
        return value
    return secrets.token_urlsafe(32)


def _env_int(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw_value = os.getenv(name)
    try:
        value = default if raw_value is None else int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")
