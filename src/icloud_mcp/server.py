"""FastMCP server entry point."""

from __future__ import annotations

from icloud_mcp import __version__
from icloud_mcp.config import Settings
from icloud_mcp.db.connection import Database, open_db
from icloud_mcp.db.repositories import ensure_defaults
from icloud_mcp.sync.scheduler import SyncScheduler
from icloud_mcp.tools.calendar_tools import register_calendar_tools
from icloud_mcp.tools.contact_tools import register_contact_tools
from icloud_mcp.tools.mail_tools import register_mail_tools
from icloud_mcp.tools.search_tools import register_search_tools
from icloud_mcp.tools.sync_tools import register_sync_tools


def create_server(settings: Settings | None = None, db: Database | None = None) -> object:
    """Create and register the FastMCP server."""

    from fastmcp import FastMCP

    active_settings = settings or Settings.from_env()
    active_db = db or open_db(active_settings.database_path)
    ensure_defaults(active_db, active_settings)

    mcp = FastMCP(
        name="iCloud MCP",
        instructions=(
            "Search and view the user's iCloud Mail, Calendar, and Contacts from a local cache. "
            "Most tools are read-only. Only calendar create/update tools modify Calendar data."
        ),
        version=__version__,
        mask_error_details=True,
    )

    register_search_tools(mcp, active_db, active_settings)
    register_mail_tools(mcp, active_db, active_settings)
    register_contact_tools(mcp, active_db, active_settings)
    register_calendar_tools(mcp, active_db, active_settings)
    register_sync_tools(mcp, active_db, active_settings)
    return mcp


def main() -> None:
    """Run the configured FastMCP server."""

    settings = Settings.from_env()
    db = open_db(settings.database_path)
    ensure_defaults(db, settings)
    scheduler = SyncScheduler(db=db, settings=settings)
    scheduler.start_background()
    mcp = create_server(settings=settings, db=db)
    if settings.transport == "http":
        mcp.run(transport="http", host=settings.host, port=settings.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
