# iCloud MCP Server

*Local-first MCP access to your iCloud Mail, Calendar, and Contacts.*

[![Security checks](https://img.shields.io/github/actions/workflow/status/denisdasilvarocha/icloud-mcp-server/security.yml?branch=main&label=checks&style=flat-square)](https://github.com/denisdasilvarocha/icloud-mcp-server/actions/workflows/security.yml) ![Python](https://img.shields.io/badge/python-%3E%3D3.11-3776AB?style=flat-square&logo=python&logoColor=white) ![FastMCP](https://img.shields.io/badge/FastMCP-stdio-111827?style=flat-square) ![SQLite](https://img.shields.io/badge/cache-SQLite%20%2B%20FTS-003B57?style=flat-square&logo=sqlite&logoColor=white)

iCloud MCP Server is a local FastMCP server that syncs iCloud data into a SQLite cache, indexes it for search, and exposes safe MCP tools for assistants. Search, list, and view tools read from the local cache; iCloud network access is isolated to background or manual sync.

> [!IMPORTANT]
> Use an Apple app-specific password. Do not use your primary Apple ID password.

## Highlights

- **Local-first search** across Mail, Calendar, and Contacts with SQLite FTS and cached query results.
- **STDIO-only MCP server** for local clients such as Codex, Claude Code, and Hermes Agent.
- **Read-heavy tool surface** where only calendar create/update tools write back to iCloud.
- **Background and manual sync** for IMAP, CalDAV, and CardDAV data, with checkpoints and retry state.
- **Credential boundary** that loads Apple credentials from environment variables or OS keychain fallback.
- **Small local dashboard** for sync health, worker status, metrics, and manual sync.

## Installation

```bash
git clone https://github.com/denisdasilvarocha/icloud-mcp-server.git
cd icloud-mcp-server
uv sync
```

Run the server directly:

```bash
ICLOUD_APPLE_ID="you@example.com" \
ICLOUD_APP_PASSWORD="xxxx-xxxx-xxxx-xxxx" \
uv run icloud-mcp
```

The server runs over STDIO, which is the local transport MCP clients expect.

## Configure MCP Clients

Interactive setup scripts can write project or global MCP configuration:

```bash
scripts/setup-codex-mcp.sh
scripts/setup-claude-code-mcp.sh
scripts/setup-hermes-agent-mcp.sh
scripts/setup-all-mcp.sh
```

Each script asks for your Apple ID, app-specific password, sync-on-start preference, and config scope. By default the app-specific password is stored in the OS keychain, not generated MCP config. Set `ICLOUD_SETUP_PERSIST_APP_PASSWORD=true` only if you explicitly want setup-generated config to include `ICLOUD_APP_PASSWORD`. Project-scoped config paths are added to `.git/info/exclude`.

> [!TIP]
> Use `scripts/setup-all-mcp.sh` when you want Codex, Claude Code, and Hermes Agent configured with the same settings.

## Tools

| Area | Tools |
| --- | --- |
| Search | `icloud.search`, `icloud.mail.search`, `icloud.calendar.search_events` |
| Mail | `icloud.mail.list`, `icloud.mail.view` |
| Contacts | `icloud.contacts.list`, `icloud.contacts.search`, `icloud.contacts.view` |
| Calendar | `icloud.calendar.list_calendars`, `icloud.calendar.list_events`, `icloud.calendar.view_event`, `icloud.calendar.create_event`, `icloud.calendar.update_event` |
| Sync and metrics | `icloud.sync.status`, `icloud.sync.now`, `icloud.metrics.snapshot` |
| Dashboard | `icloud.dashboard.start`, `icloud.dashboard.status`, `icloud.dashboard.stop` |

Resources:

- `mail://{message_id}`
- `calendar://{event_id}`
- `contact://{contact_id}`

Prompt:

- `icloud_search_prompt(question: str)`

> [!NOTE]
> Search, list, and view tools answer from the local cache. Use `icloud.sync.now` or the dashboard to refresh iCloud data.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `ICLOUD_APPLE_ID` | unset | Apple ID / iCloud email for sync adapters. |
| `ICLOUD_APP_PASSWORD` | unset | App-specific password for sync adapters. |
| `ICLOUD_MCP_DATABASE_PATH` | `~/.local/share/icloud-mcp/icloud-mcp.sqlite3` | SQLite cache path. |
| `ICLOUD_MCP_CURSOR_SECRET` | generated per process | HMAC secret for paginated cursors. Set a stable value to preserve cursors across restarts. |
| `ICLOUD_MCP_USE_KEYCHAIN` | `true` | Read app password from OS keychain when env password is absent. |
| `ICLOUD_MCP_SYNC_ON_START` | `true` | Start background sync when the MCP server starts. |
| `ICLOUD_MCP_SYNC_INTERVAL_SECONDS` | `900` | Background sync interval. |
| `ICLOUD_MCP_STALE_AFTER_SECONDS` | `86400` | Freshness threshold for sync status. |
| `ICLOUD_MCP_MAIL_SYNC_DAYS` | `30` | Mail sync lookback window. |
| `ICLOUD_MCP_MAIL_SYNC_LIMIT_PER_MAILBOX` | `250` | Mail sync limit per mailbox. |
| `ICLOUD_MCP_CALENDAR_PAST_MONTHS` | `24` | Calendar sync past window. |
| `ICLOUD_MCP_CALENDAR_FUTURE_MONTHS` | `36` | Calendar sync future window. |
| `ICLOUD_MCP_QUERY_CACHE_TTL_SECONDS` | `300` | Query cache TTL, clamped to 300-1800 seconds. |
| `ICLOUD_MCP_ATTACHMENT_TEXT_INDEXING` | `false` | Reserved attachment text indexing flag. |
| `ICLOUD_MCP_ALLOW_UNREDACTED_DEBUG` | `false` | Allow unredacted debug text. Keep disabled for normal use. |

> [!WARNING]
> Apple credentials are never MCP tool arguments. Keep them in environment variables, MCP client config, or the OS keychain.

## Dashboard

Start the local dashboard from an MCP client:

```text
icloud.dashboard.start
```

The dashboard picks the first available local port from `8765` through `8814` and exposes sync status, metrics, worker checkpoints, and a manual sync action.

## Development

```bash
uv sync --extra dev
uv run --extra dev ruff check .
uv run --extra dev coverage run -m unittest discover -s tests
uv run --extra dev coverage report
```

Generate the SBOM used by CI:

```bash
uv run --with cyclonedx-bom cyclonedx-py environment -o sbom.cdx.json
```

Run the opt-in live iCloud smoke test:

```bash
ICLOUD_APPLE_ID="you@example.com" \
ICLOUD_APP_PASSWORD="xxxx-xxxx-xxxx-xxxx" \
scripts/run-live-sync-smoke.sh
```

## Architecture

```text
MCP client
  -> FastMCP STDIO server
  -> thin tool handlers
  -> services and repositories
  -> SQLite cache, FTS indexes, checkpoints, metrics

Sync scheduler
  -> IMAP / CalDAV / CardDAV adapters
  -> local cache and indexes
```

Key paths:

- `src/icloud_mcp/mcp/` - server composition, resources, prompt registration, and MCP boundary helpers.
- `src/icloud_mcp/mail/` - Mail Cache tools, cache persistence, IMAP adapter, and sync worker.
- `src/icloud_mcp/calendar/` - Calendar Cache tools, cache persistence, CalDAV adapter, writes, and sync worker.
- `src/icloud_mcp/contacts/` - Contact Cache tools, cache persistence, CardDAV adapter, and sync worker.
- `src/icloud_mcp/search/` - Search Index orchestration, query policy, FTS/vector indexing, and maintenance.
- `src/icloud_mcp/sync/` - scheduler, checkpoints, delta-first helpers, and sync MCP tools.
- `src/icloud_mcp/storage/` - SQLite connection, schema, and migrations.
- `src/icloud_mcp/platform/` - configuration, credentials, redaction, metrics, audit, and shared utilities.
