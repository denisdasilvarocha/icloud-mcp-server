<div align="center">

# iCloud MCP Server

*Local-first MCP access to iCloud Mail, Calendar, and Contacts.*

[![Python](https://img.shields.io/badge/Python-%3E%3D3.11-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/) [![FastMCP](https://img.shields.io/badge/FastMCP-server-111827?style=flat-square)](https://github.com/jlowin/fastmcp) [![uv](https://img.shields.io/badge/uv-managed-654FF0?style=flat-square)](https://docs.astral.sh/uv/) [![Ruff](https://img.shields.io/badge/code_style-ruff-46A5E5?style=flat-square)](https://docs.astral.sh/ruff/) [![Version](https://img.shields.io/badge/version-0.1.0-31c48d?style=flat-square)](pyproject.toml)

[Features](#features) | [Setup](#setup) | [Tools](#mcp-tools) | [Configuration](#configuration) | [Development](#development)

</div>

`icloud-mcp-server` runs a local [FastMCP](https://github.com/jlowin/fastmcp) server that syncs iCloud Mail, Calendar, and Contacts into a SQLite cache, then exposes search, list, view, sync, dashboard, and guarded calendar-write tools to MCP clients.

Most tools are read-only and operate from the local cache. Calendar create/update tools are the only tools that write back to iCloud.

> [!IMPORTANT]
> Use an Apple app-specific password. Do not use your Apple ID account password.

## Features

- **Unified local search** across Mail, Calendar, and Contacts with pagination and date/person filters
- **Compact list/view tools** for cached mail messages, calendar events, calendars, and contacts
- **Background sync** with checkpoints, retry/backoff state, freshness reporting, and manual sync
- **Calendar writes** with validation, idempotency, audit events, and remote CalDAV persistence
- **Local dashboard** for sync health, worker state, cache counts, and metrics
- **MCP resources** for direct `mail://`, `calendar://`, and `contact://` lookups
- **Local-first storage** using SQLite at `~/.local/share/icloud-mcp/icloud-mcp.sqlite3` by default

## Setup

Install prerequisites:

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/)
- An iCloud Apple ID
- An Apple app-specific password

Clone the repository, then run one of the setup helpers:

```bash
git clone https://github.com/denisdasilvarocha/icloud-mcp-server.git
cd icloud-mcp-server

./scripts/setup-all-mcp.sh
```

Setup helpers prompt for credentials, config scope, and sync-on-start behavior. They also verify the server imports and write MCP client configuration for the selected client.

| Script | Target |
| --- | --- |
| `./scripts/setup-all-mcp.sh` | Codex, Claude Code, and Hermes Agent |
| `./scripts/setup-codex-mcp.sh` | Codex `.codex/config.toml` |
| `./scripts/setup-claude-code-mcp.sh` | Claude Code `.claude.json` or project `.mcp.json` |
| `./scripts/setup-hermes-agent-mcp.sh` | Hermes `.hermes/config.yaml` and `.hermes/.env` |

> [!NOTE]
> By default, setup stores the app-specific password in the OS keychain and keeps only `ICLOUD_APPLE_ID` in MCP config. Set `ICLOUD_SETUP_PERSIST_APP_PASSWORD=true` only if you explicitly want the password written to an environment file/config payload.

## Manual Run

The package exposes one stdio MCP entrypoint:

```bash
uv run icloud-mcp
```

Equivalent MCP client command payload:

```json
{
  "command": "uv",
  "args": ["run", "--project", "/path/to/icloud-mcp-server", "icloud-mcp"],
  "cwd": "/path/to/icloud-mcp-server",
  "env": {
    "ICLOUD_APPLE_ID": "you@example.com",
    "ICLOUD_MCP_SYNC_ON_START": "true"
  }
}
```

If keychain lookup is disabled or unavailable, also provide `ICLOUD_APP_PASSWORD`.

## MCP Tools

### Search

| Tool | Purpose |
| --- | --- |
| `icloud.search` | Search Mail, Calendar, and Contacts together |
| `icloud.mail.search` | Search only cached Mail |
| `icloud.calendar.search_events` | Search only cached Calendar events |

### Mail

| Tool | Purpose |
| --- | --- |
| `icloud.mail.list` | List compact mail rows from the local cache |
| `icloud.mail.view` | View one cached mail message |

### Contacts

| Tool | Purpose |
| --- | --- |
| `icloud.contacts.list` | List compact contact rows |
| `icloud.contacts.search` | Search contacts by local aliases/indexes |
| `icloud.contacts.view` | View one cached contact |

### Calendar

| Tool | Purpose |
| --- | --- |
| `icloud.calendar.list_calendars` | List known calendars |
| `icloud.calendar.list_events` | List cached events by time range |
| `icloud.calendar.view_event` | View one cached event |
| `icloud.calendar.create_event` | Create a Calendar event after validation |
| `icloud.calendar.update_event` | Update a non-recurring event or recurring series |

### Sync, Metrics, Dashboard

| Tool | Purpose |
| --- | --- |
| `icloud.sync.status` | Report cache freshness and worker checkpoints |
| `icloud.sync.now` | Run one iCloud sync cycle |
| `icloud.metrics.snapshot` | Return compact local metrics |
| `icloud.dashboard.start` | Start the local dashboard |
| `icloud.dashboard.status` | Return dashboard runtime status |
| `icloud.dashboard.stop` | Stop the local dashboard |

## Resources and Prompt

The server also exposes direct MCP resources:

- `mail://{message_id}`
- `calendar://{event_id}`
- `contact://{contact_id}`

It registers `icloud_search_prompt(question)` for evidence-grounded answers from local iCloud search results.

## Configuration

Runtime settings are read from environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `ICLOUD_APPLE_ID` | unset | Apple ID / iCloud email |
| `ICLOUD_APP_PASSWORD` | unset | App-specific password; optional when keychain has it |
| `ICLOUD_MCP_DATABASE_PATH` | `~/.local/share/icloud-mcp/icloud-mcp.sqlite3` | SQLite cache path |
| `ICLOUD_MCP_SYNC_ON_START` | `true` | Start background sync when server starts |
| `ICLOUD_MCP_SYNC_INTERVAL_SECONDS` | `900` | Background sync interval |
| `ICLOUD_MCP_STALE_AFTER_SECONDS` | `86400` | Cache freshness threshold |
| `ICLOUD_MCP_MAIL_SYNC_DAYS` | `30` | Mail lookback window |
| `ICLOUD_MCP_MAIL_SYNC_LIMIT_PER_MAILBOX` | `250` | Mail sync cap per mailbox |
| `ICLOUD_MCP_CALENDAR_PAST_MONTHS` | `24` | Calendar past sync window |
| `ICLOUD_MCP_CALENDAR_FUTURE_MONTHS` | `36` | Calendar future sync window |
| `ICLOUD_MCP_MAIL_INDEX_BODY_CHARS` | `16000` | Mail body characters indexed for search |
| `ICLOUD_MCP_QUERY_CACHE_TTL_SECONDS` | `300` | Query cache TTL, clamped up to 1800 |
| `ICLOUD_MCP_CURSOR_SECRET` | generated | Cursor signing secret |
| `ICLOUD_MCP_USE_KEYCHAIN` | `true` | Use OS keychain fallback for credentials |
| `ICLOUD_MCP_ATTACHMENT_TEXT_INDEXING` | `false` | Include attachment text in indexing |
| `ICLOUD_MCP_ALLOW_UNREDACTED_DEBUG` | `false` | Allow unredacted debug errors |

> [!WARNING]
> `ICLOUD_MCP_ALLOW_UNREDACTED_DEBUG=true` may expose sensitive account or iCloud response details in errors. Keep it off outside local debugging.

## Development

Install dependencies:

```bash
uv sync --extra dev
```

Run the server locally:

```bash
uv run icloud-mcp
```

Run unit tests:

```bash
uv run python -m unittest discover -s tests/unit
```

Run live iCloud smoke tests:

```bash
export ICLOUD_APPLE_ID="you@example.com"
export ICLOUD_APP_PASSWORD="app-specific-password"
./scripts/run-live-sync-smoke.sh
```

Lint and format:

```bash
uv run ruff check .
uv run ruff format .
```

## Project Layout

```text
src/icloud_mcp/
  mcp/          FastMCP server entrypoint and MCP boundary helpers
  mail/         IMAP sync, cache reads, and Mail tools
  calendar/     CalDAV sync, event cache, validation, and write service
  contacts/     CardDAV sync, contact cache, and Contacts tools
  search/       Query planning, FTS, snippets, embeddings, and ranking
  sync/         Scheduler, worker checkpoints, delta helpers, sync tools
  dashboard/    Local HTTP dashboard runtime and lifecycle tools
  storage/      SQLite connection, schema, migrations, cache state
  platform/     Settings, secrets, metrics, audit, redaction, XML helpers
tests/
  unit/         Fast local contract and behavior tests
  integration/  Opt-in live iCloud smoke test
scripts/        MCP client setup and live smoke helpers
```
