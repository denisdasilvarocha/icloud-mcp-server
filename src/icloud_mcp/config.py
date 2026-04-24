"""Configuration loaded outside the MCP tool surface."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DATABASE_PATH = Path("~/.local/share/icloud-mcp/icloud-mcp.sqlite3").expanduser()


@dataclass(frozen=True)
class Settings:
    """Runtime settings for local STDIO deployment."""

    database_path: Path = DEFAULT_DATABASE_PATH
    cursor_secret: str = "local-dev-cursor-secret-change-me"
    apple_id: str | None = None
    app_password: str | None = None
    default_account_id: str = "local"
    default_calendar_id: str = "cal_primary"
    default_addressbook_id: str = "addr_default"
    mail_body_view_chars: int = 8000
    mail_index_body_chars: int = 16000
    snippet_chars: int = 360
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

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from environment variables."""

        database_path = Path(os.getenv("ICLOUD_MCP_DATABASE_PATH", str(DEFAULT_DATABASE_PATH))).expanduser()
        return cls(
            database_path=database_path,
            cursor_secret=os.getenv("ICLOUD_MCP_CURSOR_SECRET", "local-dev-cursor-secret-change-me"),
            apple_id=os.getenv("ICLOUD_APPLE_ID"),
            app_password=os.getenv("ICLOUD_APP_PASSWORD"),
            mail_index_body_chars=int(os.getenv("ICLOUD_MCP_MAIL_INDEX_BODY_CHARS", "16000")),
            sync_on_start=os.getenv("ICLOUD_MCP_SYNC_ON_START", "true").lower() != "false",
            sync_interval_seconds=int(os.getenv("ICLOUD_MCP_SYNC_INTERVAL_SECONDS", "900")),
            stale_after_seconds=int(os.getenv("ICLOUD_MCP_STALE_AFTER_SECONDS", "86400")),
            mail_sync_days=int(os.getenv("ICLOUD_MCP_MAIL_SYNC_DAYS", "30")),
            mail_sync_limit_per_mailbox=int(os.getenv("ICLOUD_MCP_MAIL_SYNC_LIMIT_PER_MAILBOX", "250")),
            calendar_past_months=int(os.getenv("ICLOUD_MCP_CALENDAR_PAST_MONTHS", "24")),
            calendar_future_months=int(os.getenv("ICLOUD_MCP_CALENDAR_FUTURE_MONTHS", "36")),
            use_keychain=os.getenv("ICLOUD_MCP_USE_KEYCHAIN", "true").lower() != "false",
            attachment_text_indexing=os.getenv("ICLOUD_MCP_ATTACHMENT_TEXT_INDEXING", "false").lower() == "true",
            allow_unredacted_debug=os.getenv("ICLOUD_MCP_ALLOW_UNREDACTED_DEBUG", "false").lower() == "true",
        )
