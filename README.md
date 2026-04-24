# iCloud MCP Server

Local-first FastMCP server for iCloud Mail, Calendar, and Contacts. It keeps a SQLite cache and FTS index so MCP tools answer from local data first, while protocol adapters and sync workers isolate iCloud network access.

## Status

This repository implements the design scaffold and local MVP:

- FastMCP tool registration for search, mail, contacts, calendar, and sync status.
- SQLite WAL schema for accounts, mail, calendar, contacts, aliases, search documents, chunks, FTS, cursors, idempotency, sync checkpoints, and audit events.
- Local FTS search with compact result shapes and freshness metadata.
- Calendar create/update guardrails with idempotency and ETag conflict handling.
- Stub IMAP, CalDAV, and CardDAV adapters ready for live sync implementation.

## Run

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .
icloud-mcp
```

The server runs over local STDIO only.

## Configure MCP Clients

Interactive setup scripts are in `scripts/`:

```bash
scripts/setup-codex-mcp.sh
scripts/setup-claude-code-mcp.sh
scripts/setup-hermes-agent-mcp.sh
scripts/setup-all-mcp.sh
```

Each script asks for Apple ID, app-specific password, whether sync should run on MCP server start, and whether to write current-project or global config. Project-scoped scripts add generated secret config paths to `.git/info/exclude`.

## Configuration

Environment variables:

- `ICLOUD_MCP_DATABASE_PATH`: SQLite path. Defaults to `~/.local/share/icloud-mcp/icloud-mcp.sqlite3`.
- `ICLOUD_MCP_CURSOR_SECRET`: HMAC secret for cursors.
- `ICLOUD_APPLE_ID`: Apple account identifier for out-of-band sync adapters.
- `ICLOUD_APP_PASSWORD`: App-specific password for out-of-band sync adapters.

Apple credentials are never accepted as MCP tool arguments.

## Test

```bash
python -m unittest discover -s tests
```
