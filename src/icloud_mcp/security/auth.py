"""HTTP authorization boundary."""

from __future__ import annotations


def require_http_auth_configured(transport: str, token: str | None) -> None:
    """Require a token for HTTP deployments."""

    if transport == "http" and not token:
        raise ValueError("HTTP transport requires token verification configuration")
