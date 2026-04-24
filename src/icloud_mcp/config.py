"""Configuration loaded outside the MCP tool surface."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DEFAULT_DATABASE_PATH = Path("~/.local/share/icloud-mcp/icloud-mcp.sqlite3").expanduser()


@dataclass(frozen=True)
class Settings:
    """Runtime settings for local STDIO or private HTTP deployment."""

    database_path: Path = DEFAULT_DATABASE_PATH
    transport: Literal["stdio", "http"] = "stdio"
    host: str = "127.0.0.1"
    port: int = 8000
    cursor_secret: str = "local-dev-cursor-secret-change-me"
    apple_id: str | None = None
    app_password: str | None = None
    default_account_id: str = "local"
    default_calendar_id: str = "cal_primary"
    default_addressbook_id: str = "addr_default"
    mail_body_view_chars: int = 8000
    snippet_chars: int = 360
    sync_on_start: bool = True
    sync_interval_seconds: int = 900
    mail_sync_days: int = 30
    mail_sync_limit_per_mailbox: int = 250
    calendar_past_months: int = 24
    calendar_future_months: int = 36

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from environment variables."""

        transport = os.getenv("ICLOUD_MCP_TRANSPORT", "stdio").lower()
        if transport not in {"stdio", "http"}:
            raise ValueError("ICLOUD_MCP_TRANSPORT must be 'stdio' or 'http'")

        database_path = Path(os.getenv("ICLOUD_MCP_DATABASE_PATH", str(DEFAULT_DATABASE_PATH))).expanduser()
        return cls(
            database_path=database_path,
            transport=transport,  # type: ignore[arg-type]
            host=os.getenv("ICLOUD_MCP_HOST", "127.0.0.1"),
            port=int(os.getenv("ICLOUD_MCP_PORT", "8000")),
            cursor_secret=os.getenv("ICLOUD_MCP_CURSOR_SECRET", "local-dev-cursor-secret-change-me"),
            apple_id=os.getenv("ICLOUD_APPLE_ID"),
            app_password=os.getenv("ICLOUD_APP_PASSWORD"),
            sync_on_start=os.getenv("ICLOUD_MCP_SYNC_ON_START", "true").lower() != "false",
            sync_interval_seconds=int(os.getenv("ICLOUD_MCP_SYNC_INTERVAL_SECONDS", "900")),
            mail_sync_days=int(os.getenv("ICLOUD_MCP_MAIL_SYNC_DAYS", "30")),
            mail_sync_limit_per_mailbox=int(os.getenv("ICLOUD_MCP_MAIL_SYNC_LIMIT_PER_MAILBOX", "250")),
            calendar_past_months=int(os.getenv("ICLOUD_MCP_CALENDAR_PAST_MONTHS", "24")),
            calendar_future_months=int(os.getenv("ICLOUD_MCP_CALENDAR_FUTURE_MONTHS", "36")),
        )
