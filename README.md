<div align="center">

# iCloud MCP Server

*Local-first MCP access to iCloud Mail, Calendar, and Contacts.*

[![Python](https://img.shields.io/badge/Python-%3E%3D3.11-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/) [![FastMCP](https://img.shields.io/badge/FastMCP-server-111827?style=flat-square)](https://github.com/jlowin/fastmcp) [![uv](https://img.shields.io/badge/uv-managed-654FF0?style=flat-square)](https://docs.astral.sh/uv/) [![Ruff](https://img.shields.io/badge/code_style-ruff-46A5E5?style=flat-square)](https://docs.astral.sh/ruff/) [![Version](https://img.shields.io/badge/version-0.1.0-31c48d?style=flat-square)](pyproject.toml)

[Features](#features) | [Setup](#setup) | [Tools](#mcp-tools) | [Configuration](#configuration)

<img src=".github/assets/dashboard.png">
</div>

`icloud-mcp-server` runs a local [FastMCP](https://github.com/jlowin/fastmcp) server for iCloud Mail, Calendar, and Contacts. It syncs data into SQLite, then exposes MCP tools for search, list/view, sync, dashboard, and calendar create/update.

Mail uses IMAP. Calendar uses CalDAV. Contacts use CardDAV.

Most tools are read-only and operate from the local cache. Calendar create/update tools are the only tools that write back to iCloud.

> [!IMPORTANT]
> Use an Apple app-specific password. Do not use your Apple ID account password. See https://account.apple.com/

## Features

- Search Mail, Calendar, and Contacts with pagination and date/person filters
- List and view cached mail messages, calendar events, calendars, and contacts
- Sync in the background with checkpoints, retry state, freshness reports, and manual sync
- Create and update calendar events with validation, audit events, and CalDAV persistence
- Open a local dashboard for sync health, worker state, cache counts, and metrics
- Store the local cache in SQLite at `~/.local/share/icloud-mcp/icloud-mcp.sqlite3` by default
- Store the Apple app-specific password in the OS keychain instead of MCP config

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
| `icloud.mail.list` | List mail rows from the local cache |
| `icloud.mail.view` | View one cached mail message |

### Contacts

| Tool | Purpose |
| --- | --- |
| `icloud.contacts.list` | List contact rows |
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
| `icloud.metrics.snapshot` | Return local metrics |
| `icloud.dashboard.start` | Start the local dashboard |
| `icloud.dashboard.status` | Return dashboard runtime status |
| `icloud.dashboard.stop` | Stop the local dashboard |

> [!NOTE]
> To start the Dashboard, ask your Agent: `Start the iCloud MCP server dashboard`. It will return a local dashboard link with an access token you can visit.

## Setup

Install prerequisites:

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) or [`docker`](https://www.docker.com/)
- iCloud Apple ID (e.g. email@icloud.com)
- Apple app-specific password. Create one at https://account.apple.com/

Clone the repository, then run setup:

```bash
git clone https://github.com/denisdasilvarocha/icloud-mcp-server.git
cd icloud-mcp-server

./scripts/setup.sh
```

`scripts/setup.sh` prompts for the MCP client, credentials, config scope, credential storage, and sync-on-start behavior. It writes MCP client configuration for the selected target.

| Target | Command |
| --- | --- |
| Interactive (Recommended) | `./scripts/setup.sh` |
| Codex | `./scripts/setup.sh codex` |
| Claude Code | `./scripts/setup.sh claude-code` |
| Hermes Agent | `./scripts/setup.sh hermes-agent` |
| Docker Compose | `./scripts/setup.sh docker` |

> [!NOTE]
> If you choose keychain storage, setup keeps only `ICLOUD_APPLE_ID` in MCP config.

## Docker Compose (Recommended)

Build and start the Docker container:

```bash
./scripts/setup.sh docker
```

Compose stores the SQLite cache in the `icloud-mcp-data` volume at `/data/icloud-mcp.sqlite3`. The container is named `icloud-mcp-server` and stays running in the background. MCP clients connect with `docker exec -i icloud-mcp-server icloud-mcp`.

Docker cannot read the host keychain. Provide `ICLOUD_APP_PASSWORD` through setup (`scripts/setup.sh`) or pass an equivalent secret through your own Compose override.

The dashboard is published on host loopback ports `8765-8814`, so dashboard links returned by the MCP tool open from the host browser.

MCP client command payload for Docker:

```json
{

  "command": "docker",
  "args": ["exec", "-i", "icloud-mcp-server", "icloud-mcp"]
}
```

## Manual Run

The package exposes one stdio MCP entrypoint:

```bash
uv run icloud-mcp
```

MCP client command payload:

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

If keychain is disabled or unavailable, also provide `ICLOUD_APP_PASSWORD`.

## Configuration

Runtime settings are read from environment variables where you define the MCP Server:

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
| `ICLOUD_MCP_DASHBOARD_HOST` | `127.0.0.1` | Dashboard bind host |
| `ICLOUD_MCP_DASHBOARD_PUBLIC_HOST` | `127.0.0.1` | Hostname shown in dashboard URLs |
| `ICLOUD_MCP_DASHBOARD_PORT` | `8765` | First dashboard port to try |
| `ICLOUD_MCP_DASHBOARD_ALLOW_EXTERNAL_BIND` | `false` | Allow non-loopback dashboard bind host; keep public host loopback |
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
scripts/        Single MCP client setup script
```
