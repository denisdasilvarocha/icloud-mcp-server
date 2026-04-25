# AGENTS.md

This file provides guidance when working with code in this repository.

## Project
iCloud MCP Server — local-first FastMCP server for cached iCloud Mail, Calendar, and Contacts search.

## Tech Stack
- Python 3.11+ package using `pyproject.toml` and `uv.lock`
- FastMCP STDIO server; console entry point is `icloud-mcp = "icloud_mcp.server:main"`
- SQLite local cache with FTS, WAL, migrations, checkpoints, query cache, metrics, and audit tables
- iCloud protocols via IMAP, CalDAV, and CardDAV adapters
- Credentials from env vars or OS keychain fallback; Apple credentials are never MCP tool arguments
- Key libraries: `fastmcp`, `pydantic`, `imapclient`, `caldav`, `vobject`, `icalendar`, `httpx`, `keyring`, `orjson`, `tenacity`
- CI uses GitHub Actions for Ruff, unit tests, and SBOM generation

## Structure
- `src/icloud_mcp/server.py` — FastMCP composition root, tool/resource registration, STDIO runtime
- `src/icloud_mcp/config.py` — env parsing and runtime defaults
- `src/icloud_mcp/db/` — SQLite connection, schema, migrations, repositories
- `src/icloud_mcp/tools/` — MCP tool registration for search, mail, contacts, calendar, and sync
- `src/icloud_mcp/services/` — service-level orchestration, especially local search
- `src/icloud_mcp/adapters/` — IMAP, CalDAV, CardDAV, and DAV XML integration code
- `src/icloud_mcp/sync/` — background/manual sync scheduler, workers, checkpoints
- `src/icloud_mcp/indexing/` — chunking, FTS, query planning, embeddings, reranking
- `src/icloud_mcp/security/` — credential loading and redaction
- `scripts/` — interactive MCP client setup scripts for Codex, Claude Code, Hermes, and all clients
- `docs/design/` — design documentation
- `tests/unit/` — `unittest` suite for local cache, protocol adapters, and sync workers

## Commands
- Install: `uv sync`
- Dev run: `uv run icloud-mcp`
- Test: `uv run python -m unittest discover -s tests`
- Lint: `uv run ruff check .`
- Lint fix: `uv run ruff check . --fix`
- Format: `uv run ruff format .`
- SBOM: `uv run --with cyclonedx-bom cyclonedx-py environment -o sbom.cdx.json`
- MCP client setup: `scripts/setup-codex-mcp.sh`, `scripts/setup-claude-code-mcp.sh`, `scripts/setup-hermes-agent-mcp.sh`, or `scripts/setup-all-mcp.sh`

## Verification
After every code change, run in this order:
1. `uv run ruff check .` — fix lint/import issues
2. `uv run python -m unittest discover -s tests` — fix failing tests
3. `uv run --with cyclonedx-bom cyclonedx-py environment -o sbom.cdx.json` — run for dependency/security workflow changes

## Conventions
- Use `uv` for package and command execution in this repo.
- Keep the server STDIO-only unless the request explicitly changes transport behavior.
- Keep tool handlers thin: MCP tools should call services/repositories/adapters instead of owning domain logic.
- Preserve local-first behavior: search/list/view tools should answer from SQLite cache, not direct iCloud network calls.
- Calendar create/update tools are the only intended write surface; there are intentionally no delete tools or offline write queue.
- Use `from __future__ import annotations`, Python 3.11 built-in generics, `|` unions, and concise docstrings matching existing modules.
- Return deterministic status dictionaries for user-facing failures; redact user-facing error text where credentials or private content could appear.
- Keep settings in `Settings.from_env`; document new `ICLOUD_*` vars in `README.md`.
- Use additive SQLite migrations in `src/icloud_mcp/db/connection.py`; do not rewrite existing schema in ways that break local caches.
- Treat mail bodies, calendar descriptions, contact notes, snippets, and MCP resource payloads as untrusted user data.
- For design intent and implementation gaps, read `README.md`, `DESIGN_SPEC_GAPS.md`, and `docs/design/design.md`.

## Don't
- Don't commit local secrets or generated MCP client config: `.env*`, `.codex/config.toml`, `.mcp.json`, or `.hermes/`.
- Don't commit local caches, databases, or build artifacts: `.venv/`, `.ruff_cache/`, `__pycache__/`, `*.sqlite*`, `dist/`, or `build/`.
- Don't accept Apple ID or app-specific passwords as MCP tool arguments; use env vars, setup scripts, or keychain fallback.
- Don't add public HTTP transport, calendar delete tools, or offline write queueing unless explicitly requested.
- Don't bypass repositories for DB access from tools; keep DB boundaries centralized.
- Don't make network sync part of search/list/view paths; refresh is background/manual sync behavior.
- Don't expose unredacted debug output unless `ICLOUD_MCP_ALLOW_UNREDACTED_DEBUG` behavior is intentionally being changed.
