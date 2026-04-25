"""FastMCP dashboard tool registration."""

from __future__ import annotations

from icloud_mcp.dashboard import DASHBOARD_WRITE_ANNOTATIONS, READ_ANNOTATIONS, DashboardRuntime


def register_dashboard_tools(mcp: object, dashboard: DashboardRuntime) -> None:
    """Register local dashboard lifecycle tools."""

    @mcp.tool(name="icloud.dashboard.start", annotations=DASHBOARD_WRITE_ANNOTATIONS)
    async def dashboard_start() -> dict:
        """Start the local iCloud MCP dashboard."""

        return dashboard.start()

    @mcp.tool(name="icloud.dashboard.status", annotations=READ_ANNOTATIONS)
    async def dashboard_status() -> dict:
        """Return local dashboard status."""

        return dashboard.status()

    @mcp.tool(name="icloud.dashboard.stop", annotations=DASHBOARD_WRITE_ANNOTATIONS)
    async def dashboard_stop() -> dict:
        """Stop the local iCloud MCP dashboard."""

        return dashboard.stop()
