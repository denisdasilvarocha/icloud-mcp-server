"""FastMCP sync status tool registration."""

from __future__ import annotations

from icloud_mcp.config import Settings
from icloud_mcp.db.connection import Database
from icloud_mcp.db.repositories import sync_status
from icloud_mcp.sync.scheduler import SyncScheduler
from icloud_mcp.tools.search_tools import READ_ANNOTATIONS

SYNC_ANNOTATIONS = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}


def register_sync_tools(mcp: object, db: Database, settings: Settings) -> None:
    """Register sync status tools."""

    @mcp.tool(name="icloud.sync.status", annotations=READ_ANNOTATIONS)
    async def sync_status_tool() -> dict:
        """Report local sync freshness and worker checkpoints."""

        _ = settings
        return sync_status(db)

    @mcp.tool(name="icloud.sync.now", annotations=SYNC_ANNOTATIONS)
    async def sync_now_tool() -> dict:
        """Run one iCloud sync cycle using out-of-band credentials."""

        scheduler = SyncScheduler(db=db, settings=settings)
        return {"results": scheduler.sync_now(), "status": sync_status(db)}
