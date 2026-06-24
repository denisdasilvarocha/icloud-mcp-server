# AGENTS.md

This file provides guidance when working with code in this repository.

## Project
iCloud MCP Server - local-first FastMCP server for cached iCloud Mail, Calendar, and Contacts search.

## Tech Stack
- Python >=3.11
- FastMCP server exposed as `icloud-mcp`
- SQLite cache at `~/.local/share/icloud-mcp/icloud-mcp.sqlite3` by default
- iCloud Mail/Calendar/Contacts via IMAP, CalDAV, and CardDAV-related libraries
- Pydantic, pydantic-settings, aiosqlite, keyring, httpx, orjson, tenacity, sqlite-vec
- Ruff for linting/formatting, unittest for tests, coverage with 100% report threshold
- Build backend: Hatchling

## Structure
- `src/icloud_mcp/mcp/` - FastMCP server entrypoint and MCP boundary helpers
- `src/icloud_mcp/mail/` - IMAP sync, cache reads, and Mail tools
- `src/icloud_mcp/calendar/` - CalDAV sync, event cache, validation, and guarded writes
- `src/icloud_mcp/contacts/` - CardDAV sync, contact cache, and Contacts tools
- `src/icloud_mcp/search/` - query planning, FTS, snippets, embeddings, and ranking
- `src/icloud_mcp/sync/` - scheduler, worker checkpoints, delta helpers, and sync tools
- `src/icloud_mcp/dashboard/` - local dashboard runtime and lifecycle tools
- `src/icloud_mcp/storage/` - SQLite connection, schema, migrations, and cache state
- `src/icloud_mcp/platform/` - settings, secrets, metrics, audit, redaction, and XML helpers
- `tests/unit/` - fast local contract and behavior tests
- `tests/integration/` - opt-in live iCloud smoke test
- `scripts/` - MCP client setup and live smoke helpers

## Commands
- Setup: `uv sync --extra dev`
- Dev: `uv run icloud-mcp`
- Docker: `docker compose up -d`
- Test: `uv run python -m unittest discover -s tests/unit`
- Lint: `uv run ruff check .`
- Format: `uv run ruff format .`

## Verification
After every code change, run in this order:
1. `uv run python -m unittest discover -s tests/unit` - fix failing behavior or contract tests
2. `uv run ruff check .` - fix lint/import issues
3. `uv run ruff format .` - format only after tests and lint are clean

Run `./scripts/run-live-sync-smoke.sh` only for changes that need real iCloud verification. It requires `ICLOUD_APPLE_ID`, an app-specific password, and live-test opt-in behavior from the script.

## Conventions
- Use `uv` for Python package management and command execution.
- Keep imports ordered stdlib, third-party, local; most Python modules use `from __future__ import annotations`.
- Use typed Python directly: `dict[str, Any]`, `list[str] | None`, `tuple[...] | None`, and small dataclasses where useful.
- Register MCP tools through `register_*_tools` functions with nested `@mcp.tool(...)` closures.
- Preserve structured tool response shapes. Boundary helpers return error dictionaries or `ToolResult` instead of leaking raw exceptions.
- Preserve public MCP contract names, argument names, annotations, and schema keys; `tests/unit/test_mcp_contracts.py` is the guardrail.
- Keep calendar writes guarded by validation, idempotency, audit events, and remote CalDAV persistence.
- Keep most tools local-cache read-only. Calendar create/update are the write paths.
- Prefer small `unittest.TestCase` tests with deterministic fakes over live services.
- For runtime settings, update `src/icloud_mcp/platform/config.py` and README together.
- Docker runs stdio by default, persists cache in the `icloud-mcp-data` volume, and disables OS keychain lookup unless explicitly overridden.
