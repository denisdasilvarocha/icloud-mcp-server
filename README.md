# iCloud MCP Server

Local-first FastMCP server for iCloud Mail, Calendar, and Contacts. It keeps a SQLite cache and FTS index so MCP tools answer from local data first, while protocol adapters and sync workers isolate iCloud network access.

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
- `ICLOUD_MCP_QUERY_CACHE_TTL_SECONDS`: Search query cache TTL, clamped to 300-1800 seconds. Defaults to `300`.
- `ICLOUD_APPLE_ID`: Apple account identifier for out-of-band sync adapters.
- `ICLOUD_APP_PASSWORD`: App-specific password for out-of-band sync adapters.
- `ICLOUD_MCP_USE_KEYCHAIN`: Read the app-specific password from the OS keychain when env password is absent. Defaults to `true`.
- `ICLOUD_MCP_STALE_AFTER_SECONDS`: Freshness threshold for stale sync status. Defaults to one day.
- `ICLOUD_MCP_ATTACHMENT_TEXT_INDEXING`: Reserved flag for future attachment text extraction. Defaults to `false`.

Apple credentials are never accepted as MCP tool arguments.

## Operational Limits

- Search/list/view always answer from local cache; `refresh_if_stale` reports stale domains because refresh runs through background/manual sync.
- Mail backfill is checkpointed per mailbox, but older body backfill remains bounded by configured sync windows.
- Cursor-taking tools return deterministic `invalid_cursor` status objects for malformed, tampered, or expired cursors.
- Retrieved mail bodies, calendar descriptions, contact notes, snippets, and resource payloads are untrusted user data and are labeled as such in outputs.
- Setup scripts can still write env files; prefer OS keychain storage for app-specific passwords.

## Security Checks

```bash
uv run --extra dev ruff check .
uv run --extra dev coverage run -m unittest discover -s tests
uv run --extra dev coverage report
uv run --with cyclonedx-bom cyclonedx-py environment -o sbom.cdx.json
```

Current fake protocol and contract coverage lives in `tests/unit/test_protocol_adapters.py`,
`tests/unit/test_sync_workers.py`, `tests/unit/test_mcp_contracts.py`, and
`tests/unit/test_spec_closure.py`.

## Test

```bash
uv run --extra dev coverage run -m unittest discover -s tests
uv run --extra dev coverage report
```
