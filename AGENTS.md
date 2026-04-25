# AGENTS.md

This file provides guidance when working with code in this repository.

## Project
iCloud MCP Server - local-first FastMCP server for cached iCloud Mail, Calendar, and Contacts search.

## Tech Stack
- Python >=3.11, packaged with Hatchling and locked with `uv.lock`
- FastMCP over STDIO
- SQLite cache with FTS5 and `sqlite-vec`
- IMAP, CalDAV, and CardDAV adapters for iCloud sync
- Apple app-specific password auth via environment variables or OS keychain fallback
- Ruff for linting/formatting; `unittest` plus `coverage` for tests

## Structure
- `src/icloud_mcp/server.py` - FastMCP composition, CLI entry point, resources, and prompts.
- `src/icloud_mcp/config.py` - environment-driven runtime settings and defaults.
- `src/icloud_mcp/tools/` - MCP tool registration for search, mail, calendar, contacts, sync, and dashboard.
- `src/icloud_mcp/services/` - service-level orchestration, especially search policy.
- `src/icloud_mcp/db/` - SQLite connection, schema, migrations, and repositories.
- `src/icloud_mcp/sync/` - background/manual sync scheduler, workers, and checkpoints.
- `src/icloud_mcp/adapters/` - IMAP, CalDAV, CardDAV, and DAV XML protocol code.
- `src/icloud_mcp/indexing/` - query planning, FTS/vector search, chunking, embeddings, and reranking.
- `src/icloud_mcp/security/` - credential boundary and redaction helpers.
- `src/icloud_mcp/observability/` - audit, metrics, and logging helpers.
- `scripts/` - setup scripts for Codex, Claude Code, Hermes Agent, all clients, and live sync smoke tests.
- `tests/unit/` - contract, dashboard, sync, protocol, edge, and local MVP tests.
- `tests/integration/` - opt-in live iCloud sync smoke tests.

## Commands
- Install: `uv sync`
- Install dev deps: `uv sync --extra dev`
- Run MCP server: `uv run icloud-mcp`
- Lint: `uv run --extra dev ruff check .`
- Test: `uv run --extra dev coverage run -m unittest discover -s tests`
- Coverage report: `uv run --extra dev coverage report`
- Live smoke test: `scripts/run-live-sync-smoke.sh`
- Generate SBOM: `uv run --with cyclonedx-bom cyclonedx-py environment -o sbom.cdx.json`
- Setup MCP clients: `scripts/setup-codex-mcp.sh`, `scripts/setup-claude-code-mcp.sh`, `scripts/setup-hermes-agent-mcp.sh`, or `scripts/setup-all-mcp.sh`

## Verification
After every change, run in this order:
1. `uv run --extra dev ruff check .` - fix lint/import/style issues.
2. `uv run --extra dev coverage run -m unittest discover -s tests` - fix failing tests.
3. `uv run --extra dev coverage report` - maintain the configured 100% coverage threshold.

For iCloud adapter or sync changes, also run `scripts/run-live-sync-smoke.sh` only when `ICLOUD_APPLE_ID` and `ICLOUD_APP_PASSWORD` are intentionally available.

## Conventions
- Keep MCP transport STDIO-only; the dashboard is a separate localhost utility started through dashboard tools.
- Use `Settings.from_env()` as the configuration boundary; add new env parsing there instead of scattered `os.getenv` calls.
- Keep Apple credentials out of MCP tool arguments; credentials belong in environment variables, client config, or OS keychain.
- Tool handlers should return deterministic structured statuses for user-facing errors, especially cursor and not-found paths.
- Preserve public MCP argument names and annotations; contract tests cover names like `freshness`, `from`, and read-only hints.
- Prefer local-cache behavior for search/list/view tools; network access should stay in sync workers or explicit calendar write paths.
- Use `from __future__ import annotations`, absolute `icloud_mcp.*` imports, dataclasses where appropriate, and typed function signatures.
- Ruff config uses line length 120, double quotes, and lint codes `E`, `W`, `F`, `I`, `B`, `C4`, `UP`, and `SIM`.
- Coverage is configured with `fail_under = 100`; add focused tests with behavior changes.

## Environment
- `ICLOUD_APPLE_ID`
- `ICLOUD_APP_PASSWORD`
- `ICLOUD_MCP_DATABASE_PATH`
- `ICLOUD_MCP_CURSOR_SECRET`
- `ICLOUD_MCP_USE_KEYCHAIN`
- `ICLOUD_MCP_SYNC_ON_START`
- `ICLOUD_MCP_SYNC_INTERVAL_SECONDS`
- `ICLOUD_MCP_STALE_AFTER_SECONDS`
- `ICLOUD_MCP_MAIL_SYNC_DAYS`
- `ICLOUD_MCP_MAIL_SYNC_LIMIT_PER_MAILBOX`
- `ICLOUD_MCP_CALENDAR_PAST_MONTHS`
- `ICLOUD_MCP_CALENDAR_FUTURE_MONTHS`
- `ICLOUD_MCP_QUERY_CACHE_TTL_SECONDS`
- `ICLOUD_MCP_ATTACHMENT_TEXT_INDEXING`
- `ICLOUD_MCP_ALLOW_UNREDACTED_DEBUG`
- `ICLOUD_MCP_MAIL_INDEX_BODY_CHARS`
- `ICLOUD_MCP_LIVE_TESTS`

## Don't
- Don't use a primary Apple ID password - use an Apple app-specific password.
- Don't add Apple credentials to MCP tool schemas, test fixtures, logs, or committed config.
- Don't change public tool argument names casually - `tests/unit/test_mcp_contracts.py` protects the MCP contract.
- Don't turn cache-only read tools into implicit network callers - use `icloud.sync.now` or sync workers for refresh.
- Don't bypass repository/search service boundaries for DB behavior unless the surrounding module already owns that query.
- Don't run live sync smoke tests without explicit credentials and an isolated database path.
