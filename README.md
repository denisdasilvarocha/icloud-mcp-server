# iCloud MCP Server

Local-first FastMCP server for iCloud Mail, Calendar, and Contacts. It keeps a SQLite cache and FTS index so MCP tools answer from local data first, while protocol adapters and sync workers isolate iCloud network access.

## Status

Design gap tracking lives in [DESIGN_SPEC_GAPS.md](DESIGN_SPEC_GAPS.md).

Implemented:

- FastMCP tool registration for search, mail, contacts, calendar, and sync status.
- Optional MCP resources for `mail://{message_id}`, `calendar://{event_id}`, and `contact://{contact_id}` plus an iCloud search prompt.
- SQLite WAL schema with additive migrations for accounts, mail, calendar, contacts, aliases, chunks, FTS, durable local embeddings, cursors, idempotency, sync checkpoints, metrics, and audit events.
- IMAP parsing for Bcc, threading headers, attachments, text/calendar invites, encrypted-body status, and quote-suppressed search chunks.
- Local hybrid search with query planning, relative date windows, alias expansion, chunk snippets, occurrence documents, freshness metadata, deterministic answer hints, and structured output fields.
- Calendar create/update guardrails with idempotency, ETag conflict handling, single/future/series scope handling, recurrence EXDATE/RDATE expansion, and ICS property preservation for patched fields.
- CardDAV aliases for names, email local parts, nicknames, phonetic/relation fields, plus tombstone cleanup.
- Sync status, local metrics, redaction, keychain credential fallback, and STDIO-only runtime configuration.

Partial:

- IMAP, CalDAV, and CardDAV live adapters support real network sync and store sync metadata, but protocol-level QRESYNC/WebDAV `sync-collection` behavior still depends on what the underlying libraries expose.
- Calendar remote writes preflight ETags and normalize connectivity/auth errors. Explicit `If-Match` transport headers need fake-server integration coverage before claiming full wire-level enforcement.
- Attachment text/PDF extraction is disabled by default; attachment metadata is indexed and returned.
- Vector search persists deterministic local sparse embeddings rather than requiring `sqlite-vec`.

Not implemented:

- Public HTTP transport is intentionally absent.
- Calendar delete tools are intentionally absent.
- Offline write queueing is intentionally absent.

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
- `ICLOUD_MCP_USE_KEYCHAIN`: Read the app-specific password from the OS keychain when env password is absent. Defaults to `true`.
- `ICLOUD_MCP_STALE_AFTER_SECONDS`: Freshness threshold for stale sync status. Defaults to one day.
- `ICLOUD_MCP_ATTACHMENT_TEXT_INDEXING`: Reserved flag for future attachment text extraction. Defaults to `false`.

Apple credentials are never accepted as MCP tool arguments.

## Operational Limits

- Search/list/view always answer from local cache; `refresh_if_stale` reports stale domains because refresh runs through background/manual sync.
- Mail backfill is checkpointed per mailbox, but older body backfill remains bounded by configured sync windows.
- Retrieved mail bodies, calendar descriptions, contact notes, snippets, and resource payloads are untrusted user data and are labeled as such in outputs.
- Setup scripts can still write env files; prefer OS keychain storage for app-specific passwords.

## Security Checks

```bash
uv run ruff check .
uv run python -m unittest discover -s tests
uv run --with cyclonedx-bom cyclonedx-py environment -o sbom.cdx.json
```

## Test

```bash
python -m unittest discover -s tests
```
