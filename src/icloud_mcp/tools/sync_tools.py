"""FastMCP sync status tool registration."""

from __future__ import annotations

from icloud_mcp.config import Settings
from icloud_mcp.db.cache_state import sync_status
from icloud_mcp.db.connection import Database
from icloud_mcp.observability.metrics import metrics_snapshot
from icloud_mcp.sync.scheduler import SyncScheduler
from icloud_mcp.tools.boundary import bounded_int
from icloud_mcp.tools.search_tools import READ_ANNOTATIONS

SYNC_ANNOTATIONS = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}


def register_sync_tools(
    mcp: object,
    db: Database,
    settings: Settings,
    scheduler: SyncScheduler | None = None,
) -> None:
    """Register sync status tools."""

    @mcp.tool(name="icloud.sync.status", annotations=READ_ANNOTATIONS)
    async def sync_status_tool() -> dict:
        """Report local sync freshness and worker checkpoints."""

        return sync_status(db, settings.stale_after_seconds)

    @mcp.tool(name="icloud.sync.now", annotations=SYNC_ANNOTATIONS)
    async def sync_now_tool() -> dict:
        """Run one iCloud sync cycle using out-of-band credentials."""

        active_scheduler = scheduler or SyncScheduler(db=db, settings=settings)
        return {"results": active_scheduler.sync_now(), "status": sync_status(db, settings.stale_after_seconds)}

    @mcp.tool(name="icloud.metrics.snapshot", annotations=READ_ANNOTATIONS)
    async def metrics_snapshot_tool(limit: int = 100) -> dict:
        """Return compact local metrics snapshot."""

        return metrics_snapshot(db, limit=bounded_int(limit, minimum=1, maximum=500))
