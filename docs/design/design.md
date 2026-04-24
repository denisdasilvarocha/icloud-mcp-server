# iCloud MCP Server with FastMCP — Detailed Design Spec

## 1. Recommended approach

Build a **local-first or private-hosted FastMCP server** that talks to iCloud through standards-based protocols, keeps a local synchronized index, and serves all interactive MCP calls from the local cache/index whenever possible.

The best architecture is:

**FastMCP tools → service layer → local cache + hybrid search index → background iCloud sync workers → IMAP / CalDAV / CardDAV**

Do **not** make each MCP search call hit iCloud directly. iCloud protocol calls are network-bound, sometimes slow, and not optimized for natural-language cross-domain retrieval. Instead, synchronize Mail, Calendar, and Contacts into a local normalized store, build a hybrid lexical + semantic index, and make MCP tools query that local system.

For iCloud access, use:

| Domain   | Protocol | Mutations allowed           |
| -------- | -------: | --------------------------- |
| Mail     |     IMAP | No, read-only               |
| Calendar |   CalDAV | Create + update events only |
| Contacts |  CardDAV | No, read-only               |

Apple documents iCloud Mail as IMAP/SMTP-based, with IMAP at `imap.mail.me.com`, SSL required, port `993`, and app-specific password authentication; POP is not supported. For this design, SMTP is unnecessary because Mail is read-only. ([Apple Support][1]) Apple also documents that third-party apps accessing iCloud Mail, Calendar, and Contacts use app-specific passwords, which require two-factor authentication and can be revoked; changing the primary Apple Account password revokes app-specific passwords. ([Apple Support][2]) Apple’s own iCloud security overview says Contacts and Calendars are built on CalDAV/CardDAV standards, which is the key reason this design should use standards-based DAV instead of private iCloud web APIs. ([Apple Support][3])

FastMCP is a good fit because it exposes Python functions as MCP tools, resources, and prompts, generates schemas and validation from function signatures, supports STDIO for local desktop-style use, and supports HTTP/Streamable HTTP for remote deployments. ([FastMCP][4]) MCP itself models server capabilities as **Tools**, **Resources**, and **Prompts**, so the server should expose compact tools for operations and optional resources for object retrieval by URI. ([Model Context Protocol][5])

---

## 2. High-level architecture

```text
┌─────────────────────────────────────────────────────────────────┐
│                         MCP Client / LLM                         │
│    "What time is my meeting with Liesa?"                         │
└──────────────────────────────┬──────────────────────────────────┘
                               │ MCP tool call
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                         FastMCP Server                           │
│  Tools: search, list/view mail, list/view contacts,              │
│         list/view/search calendar, create/update event           │
│  Annotations: readOnlyHint for read tools, write hints for events│
└──────────────────────────────┬──────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                         Service Layer                            │
│  Query planner │ auth/ACL │ validation │ pagination │ redaction  │
└──────────┬───────────────────────────────┬──────────────────────┘
           ▼                               ▼
┌───────────────────────┐        ┌────────────────────────────────┐
│ Local object database  │        │ Hybrid RAG/search layer         │
│ SQLite/Postgres        │        │ FTS5/BM25 + vectors + reranker  │
│ mail/events/contacts   │        │ query cache + result snippets   │
└──────────┬────────────┘        └────────────────┬───────────────┘
           │                                      │
           ▼                                      ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Background Sync Workers                      │
│ IMAP sync │ CalDAV sync │ CardDAV sync │ indexer │ embedder       │
└──────────┬──────────────────────┬────────────────┬───────────────┘
           ▼                      ▼                ▼
┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
│ iCloud IMAP Mail │   │ iCloud CalDAV    │   │ iCloud CardDAV   │
└──────────────────┘   └──────────────────┘   └──────────────────┘
```

The important performance principle is: **interactive tools must be local-index reads; remote iCloud access should happen in background sync or explicit refresh paths only.**

---

## 3. Deployment model

### Recommended default: local STDIO server

For personal iCloud data, the safest and simplest deployment is a local FastMCP STDIO server launched by the MCP client. FastMCP uses STDIO by default, and that model is intended for local tools and desktop applications. ([FastMCP][6])

Benefits:

| Benefit                         | Reason                                                       |
| ------------------------------- | ------------------------------------------------------------ |
| Stronger privacy                | iCloud app-specific password stays on the user’s machine     |
| Lower infrastructure complexity | No public HTTP endpoint, no multi-user auth system           |
| Faster cache access             | Local SQLite/LanceDB/Qdrant storage                          |
| Easier secret storage           | macOS Keychain / Windows Credential Manager / Secret Service |

### Optional: private HTTP server

Use HTTP/Streamable HTTP only when multiple local clients or remote clients need access. FastMCP supports HTTP transport and exposes the MCP server over the network; unlike STDIO, one HTTP server can handle multiple clients simultaneously. ([FastMCP][7])

For HTTP deployments, add FastMCP token verification or OAuth-compatible authorization. FastMCP supports token verification for HTTP transports, and MCP’s authorization specification treats protected MCP servers as OAuth 2.1 resource servers that validate bearer tokens and token audiences. ([FastMCP][8])

---

## 4. Source integration design

## 4.1 Mail adapter: IMAP read-only

### Responsibilities

The Mail adapter must:

1. Discover mailboxes.
2. Synchronize message metadata.
3. Fetch bodies for indexing.
4. Fetch full message bodies only on explicit `mail.view`.
5. Never mutate mail state unless a future explicit tool is added.

### iCloud connection

```yaml
mail:
  host: imap.mail.me.com
  port: 993
  ssl: true
  username: "<icloud email or account username>"
  password_source: "keychain:icloud-mcp"
```

Apple documents these IMAP settings for iCloud Mail and requires an app-specific password for third-party client access. ([Apple Support][1])

### Recommended Python approach

Use one of these:

| Option                                | Recommendation                     |
| ------------------------------------- | ---------------------------------- |
| `imapclient` in a bounded thread pool | Best maturity/reliability tradeoff |
| `imaplib`                             | Acceptable but more manual parsing |
| Async IMAP libraries                  | Only after stability testing       |

IMAPClient has built-in support for IMAP operations such as IDLE responses and parsed results, while Python’s standard `imaplib` can fetch message parts such as `UID BODY[TEXT]`. ([imapclient.readthedocs.io][9])

### Sync algorithm

For each mailbox:

1. Connect with SSL.
2. Select mailbox read-only.
3. Track:

   * `uid_validity`
   * `uid_next`
   * `highest_modseq`, if available
   * last synced UID
   * folder name
4. Fetch new headers first:

   * UID
   * Message-ID
   * In-Reply-To
   * References
   * Date
   * From / To / Cc / Bcc
   * Subject
   * Flags
   * Size
5. Fetch text bodies in batches for indexing:

   * `text/plain`
   * sanitized `text/html` converted to text
   * small `text/calendar` invite parts
6. Do not fetch large attachments during normal sync.
7. On UID validity change, mark mailbox index stale and resync.

### Mail body edge cases

The indexer must handle:

| Edge case                             | Handling                                                                                    |
| ------------------------------------- | ------------------------------------------------------------------------------------------- |
| Query only found in body              | Index normalized body chunks, not just headers                                              |
| HTML-only emails                      | Convert HTML to text, strip scripts/styles                                                  |
| Quoted reply chains                   | Store full text, but create “cleaned reply” chunks with quoted text down-weighted           |
| Calendar invite email not on calendar | Parse `text/calendar` parts and index as `mail_invite` evidence                             |
| Attachments                           | Index metadata by default; optionally index small text/PDF attachments behind a config flag |
| Huge emails                           | Store preview and first N chunks; lazy-fetch more on explicit view                          |
| S/MIME/encrypted body                 | Index metadata only; return `body_unavailable_reason`                                       |
| Spam/newsletters                      | Lower rank unless exact match or user filters include those folders                         |

---

## 4.2 Calendar adapter: CalDAV read/write for events

### Responsibilities

The Calendar adapter must:

1. Discover calendars.
2. List events by time range.
3. View event details.
4. Search event metadata and descriptions.
5. Create events.
6. Update events.
7. Preserve ETags and iCalendar fields.
8. Handle recurrence safely.

### iCloud CalDAV connection

Use the CalDAV root URL:

```yaml
calendar:
  root_url: "https://caldav.icloud.com/"
```

The Python CalDAV library documents `https://caldav.icloud.com/` as the typical iCloud root URL and recommends letting the library discover iCloud principal and calendar URLs because iCloud calendar URLs contain provider-specific numeric IDs and hostnames. ([caldav.readthedocs.io][10]) CalDAV defines `calendar-query` REPORT for searching calendar objects and returning requested properties and calendar data. ([IETF Datatracker][11])

### Recommended Python approach

Use:

| Library                  | Purpose                                  |
| ------------------------ | ---------------------------------------- |
| `caldav`                 | Calendar discovery, fetch, create/update |
| `icalendar` or `vobject` | Parse and generate iCalendar objects     |
| `zoneinfo`               | Time zones                               |
| `dateutil.rrule`         | Recurrence expansion                     |

The `icalendar` package can create, inspect, and modify calendaring information, and `vobject` supports parsing and generating iCalendar/vCard objects. ([PyPI][12])

### Sync algorithm

For each calendar collection:

1. Discover calendars from the principal.
2. Store:

   * calendar URL
   * display name
   * color, if available
   * ETag / ctag / sync token
3. Prefer WebDAV sync-token if supported.
4. Otherwise perform time-window `calendar-query`:

   * default: now − 24 months to now + 36 months
   * configurable
5. Store raw ICS plus normalized fields.
6. Expand recurring events into occurrence rows for the configured search window.
7. Re-expand recurrence on event changes.

WebDAV sync-token and `sync-collection` are designed for efficient synchronization of changed resources in a WebDAV collection. ([IETF Datatracker][13])

### Calendar create

Create event flow:

1. Validate input.
2. Resolve calendar ID.
3. Generate UID.
4. Generate valid VEVENT.
5. PUT to CalDAV calendar collection.
6. Store returned href/ETag.
7. Trigger local sync/index update immediately.
8. Return compact structured result.

Required fields:

```json
{
  "calendar_id": "primary",
  "title": "Lunch with Liesa",
  "start": "2026-04-27T12:00:00+02:00",
  "end": "2026-04-27T13:00:00+02:00",
  "timezone": "Europe/Berlin"
}
```

Optional fields:

```json
{
  "location": "Berlin",
  "description": "Discuss project timeline",
  "attendees": [{"email": "liesa@example.com", "name": "Liesa"}],
  "recurrence": {"freq": "weekly", "count": 4},
  "alarms": [{"minutes_before": 15}],
  "request_id": "client-generated-idempotency-key"
}
```

### Calendar update

Update event flow:

1. Fetch current event by internal ID or CalDAV href.
2. Check ETag.
3. Apply patch.
4. Preserve unknown ICS properties.
5. PUT with `If-Match` ETag.
6. If ETag mismatch, return conflict with latest summary.
7. Re-index updated object.

Update scope:

| Scope    | Meaning                               |
| -------- | ------------------------------------- |
| `single` | Update only one occurrence            |
| `future` | Update this and following occurrences |
| `series` | Update entire recurring series        |

The MVP may support `series` and non-recurring events first, then add `single` and `future` recurrence exceptions.

---

## 4.3 Contacts adapter: CardDAV read-only

### Responsibilities

The Contacts adapter must:

1. Discover address books.
2. List contacts.
3. View contact details.
4. Search contacts.
5. Build alias/person lookup for search.

### iCloud CardDAV connection

Use CardDAV discovery starting from:

```yaml
contacts:
  root_url: "https://contacts.icloud.com/"
```

Apple’s platform docs describe CardDAV account configuration as connecting to a CardDAV-compliant server with hostname, port, and optional principal URL, while Apple’s iCloud security overview confirms Contacts are standards-based CardDAV data. ([Apple Support][14]) Community and sync-client documentation commonly use `contacts.icloud.com` and note that iCloud may redirect/discover per-account hostnames such as `pxxx-contacts.icloud.com`; the implementation should therefore use CardDAV discovery rather than hard-coding a final per-user URL. ([GNOME Discourse][15])

### Recommended Python approach

Use:

| Library                | Purpose                      |
| ---------------------- | ---------------------------- |
| `httpx.AsyncClient`    | WebDAV/CardDAV HTTP requests |
| `vobject`              | vCard parsing/generation     |
| `lxml` or `defusedxml` | Safe XML parsing             |

CardDAV is a WebDAV extension for accessing and managing vCard-based contact information, and WebDAV sync-token support can be used where the server supports collection sync. ([IETF Datatracker][16])

### Contact search normalization

For every contact, generate searchable aliases:

```text
Liesa Müller
Liesa
Müller
liesa.mueller@example.com
Company name
nickname
phone normalized E.164
```

Also maintain a `person_alias` table so a query like **“meeting with Liesa”** expands to:

```json
{
  "display_name": "Liesa Müller",
  "aliases": ["Liesa", "Liesa Mueller", "liesa.mueller@example.com"],
  "emails": ["liesa.mueller@example.com"]
}
```

---

# 5. MCP interface design

## 5.1 Tool design principles

Keep the tool surface small, typed, and token-efficient.

FastMCP tools automatically use function names, docstrings, parameter annotations, and type hints to generate tool schemas and validate inputs. ([FastMCP][4]) FastMCP also supports annotations such as `readOnlyHint`, `destructiveHint`, `idempotentHint`, and `openWorldHint`; read-only tools should be explicitly marked read-only, while calendar writes should not be marked read-only. ([FastMCP][4])

### Naming convention

Use namespaced tool names:

```text
icloud.search
icloud.mail.list
icloud.mail.view
icloud.mail.search
icloud.contacts.list
icloud.contacts.view
icloud.contacts.search
icloud.calendar.list_calendars
icloud.calendar.list_events
icloud.calendar.view_event
icloud.calendar.search_events
icloud.calendar.create_event
icloud.calendar.update_event
icloud.sync.status
```

Avoid exposing too many micro-tools. The model should not need to call 8 tools to answer a simple question.

---

## 5.2 Unified search tool

### `icloud.search`

Primary cross-domain RAG search.

**Use when:** the user asks a natural-language question that could involve Mail, Calendar, Contacts, or multiple domains.

```python
@mcp.tool(
    name="icloud.search",
    annotations={
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def search(
    query: str,
    domains: list[Literal["mail", "calendar", "contacts"]] = ["mail", "calendar", "contacts"],
    start: datetime | None = None,
    end: datetime | None = None,
    person: str | None = None,
    limit: int = 10,
    include_body_snippets: bool = True,
    freshness: Literal["cache_only", "allow_stale", "refresh_if_stale"] = "allow_stale",
) -> SearchResult:
    ...
```

### Output shape

```json
{
  "query": "What time is my meeting with Liesa?",
  "normalized_query": "meeting time liesa",
  "index_freshness": {
    "mail_last_sync": "2026-04-24T09:01:00+02:00",
    "calendar_last_sync": "2026-04-24T09:03:00+02:00",
    "contacts_last_sync": "2026-04-24T08:58:00+02:00"
  },
  "answer_hints": [
    {
      "type": "calendar_time",
      "confidence": 0.91,
      "text": "Likely meeting: Project Sync with Liesa, Monday 2026-04-27 14:00–15:00 Europe/Berlin",
      "source_ids": ["cal_evt_123"]
    }
  ],
  "results": [
    {
      "id": "cal_evt_123",
      "domain": "calendar",
      "title": "Project Sync with Liesa",
      "time": {
        "start": "2026-04-27T14:00:00+02:00",
        "end": "2026-04-27T15:00:00+02:00",
        "timezone": "Europe/Berlin"
      },
      "participants": ["Liesa Müller"],
      "snippet": "Project Sync with Liesa. Location: Zoom.",
      "score": 0.94,
      "why": ["exact_person_match", "calendar_intent", "upcoming_event"]
    },
    {
      "id": "mail_msg_887",
      "domain": "mail",
      "title": "Re: Project Sync",
      "date": "2026-04-21T11:23:00+02:00",
      "snippet": "Let's meet Monday at 14:00...",
      "score": 0.72,
      "why": ["body_match", "person_email_match"]
    }
  ],
  "next_cursor": null
}
```

The tool should return enough evidence for the LLM to answer, but not full message bodies unless needed.

---

## 5.3 Mail tools

### `icloud.mail.list`

Lists compact mail rows.

```json
{
  "mailbox": "INBOX",
  "after": "2026-04-01T00:00:00+02:00",
  "before": null,
  "from": null,
  "limit": 25,
  "cursor": null
}
```

Return:

```json
{
  "messages": [
    {
      "id": "mail_msg_887",
      "mailbox": "INBOX",
      "subject": "Re: Project Sync",
      "from": {"name": "Liesa Müller", "email": "liesa@example.com"},
      "date": "2026-04-21T11:23:00+02:00",
      "preview": "Let's meet Monday at 14:00...",
      "has_attachments": false
    }
  ],
  "next_cursor": "..."
}
```

### `icloud.mail.view`

Views one message.

Parameters:

```json
{
  "message_id": "mail_msg_887",
  "include": ["headers", "body_text", "attachments"],
  "max_body_chars": 8000
}
```

Return compact text plus continuation:

```json
{
  "id": "mail_msg_887",
  "subject": "Re: Project Sync",
  "headers": {...},
  "body_text": "Let's meet Monday at 14:00...",
  "body_truncated": false,
  "attachments": []
}
```

### `icloud.mail.search`

Domain-specific wrapper around `icloud.search(domains=["mail"])`.

---

## 5.4 Contact tools

### `icloud.contacts.list`

```json
{
  "addressbook_id": "default",
  "limit": 50,
  "cursor": null
}
```

### `icloud.contacts.view`

```json
{
  "contact_id": "contact_123",
  "include_notes": false
}
```

### `icloud.contacts.search`

```json
{
  "query": "Liesa",
  "limit": 10
}
```

Return:

```json
{
  "contacts": [
    {
      "id": "contact_123",
      "display_name": "Liesa Müller",
      "emails": ["liesa@example.com"],
      "phones": [],
      "organization": "Example GmbH",
      "score": 0.98
    }
  ]
}
```

---

## 5.5 Calendar tools

### `icloud.calendar.list_calendars`

Returns calendars.

```json
{
  "calendars": [
    {
      "id": "cal_primary",
      "name": "Calendar",
      "read_only": false
    }
  ]
}
```

### `icloud.calendar.list_events`

```json
{
  "calendar_ids": ["cal_primary"],
  "start": "2026-04-24T00:00:00+02:00",
  "end": "2026-05-01T00:00:00+02:00",
  "limit": 100,
  "cursor": null
}
```

### `icloud.calendar.view_event`

```json
{
  "event_id": "cal_evt_123",
  "include_raw_ics": false
}
```

### `icloud.calendar.search_events`

Domain-specific wrapper around `icloud.search(domains=["calendar"])`.

### `icloud.calendar.create_event`

```python
@mcp.tool(
    name="icloud.calendar.create_event",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True
    }
)
async def create_event(input: CreateEventInput) -> CalendarWriteResult:
    ...
```

Use `destructiveHint=False` because create is a write but not destructive. Use `openWorldHint=True` if attendees may receive invitations or the operation reaches iCloud/other people.

### `icloud.calendar.update_event`

```python
@mcp.tool(
    name="icloud.calendar.update_event",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True
    }
)
async def update_event(input: UpdateEventInput) -> CalendarWriteResult:
    ...
```

For updates, require either:

```json
{
  "event_id": "cal_evt_123",
  "patch": {"title": "Updated title"},
  "etag": "optional-known-etag",
  "scope": "series"
}
```

or a server-side conflict check if no ETag is provided.

---

# 6. Local database design

## 6.1 Storage choice

### Recommended for local/personal MCP

Use:

```text
SQLite WAL
+ SQLite FTS5
+ embedded vector index, such as LanceDB or sqlite-vec
+ optional Redis only for remote/multi-process cache
```

SQLite FTS5 provides full-text search, BM25 ranking, prefix queries, tokenizer configuration, and trigram tokenization for substring-like matching. ([SQLite][17]) SQLite WAL mode is useful here because readers do not block writers and writers do not block readers in normal WAL operation, which fits “MCP reads while background sync writes.” ([SQLite][18])

### Recommended for multi-user production

Use:

```text
PostgreSQL
+ pgvector
+ Postgres full-text search
+ Redis
+ per-user encrypted credential vault
```

This is operationally heavier but better for multi-user HTTP deployments.

---

## 6.2 Core tables

### `accounts`

```sql
CREATE TABLE accounts (
  id TEXT PRIMARY KEY,
  apple_id_hash TEXT NOT NULL,
  display_name TEXT,
  created_at TEXT NOT NULL
);
```

### `mailboxes`

```sql
CREATE TABLE mailboxes (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  name TEXT NOT NULL,
  uid_validity TEXT,
  uid_next INTEGER,
  highest_modseq TEXT,
  last_sync_at TEXT,
  UNIQUE(account_id, name)
);
```

### `mail_messages`

```sql
CREATE TABLE mail_messages (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  mailbox_id TEXT NOT NULL,
  uid INTEGER NOT NULL,
  message_id TEXT,
  thread_id TEXT,
  subject TEXT,
  from_json TEXT,
  to_json TEXT,
  cc_json TEXT,
  date TEXT,
  flags_json TEXT,
  size_bytes INTEGER,
  preview TEXT,
  body_text TEXT,
  body_hash TEXT,
  has_attachments INTEGER DEFAULT 0,
  deleted_at TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(mailbox_id, uid)
);
```

### `calendar_collections`

```sql
CREATE TABLE calendar_collections (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  url TEXT NOT NULL UNIQUE,
  display_name TEXT,
  color TEXT,
  sync_token TEXT,
  ctag TEXT,
  read_only INTEGER DEFAULT 0,
  last_sync_at TEXT
);
```

### `calendar_objects`

```sql
CREATE TABLE calendar_objects (
  id TEXT PRIMARY KEY,
  calendar_id TEXT NOT NULL,
  href TEXT NOT NULL,
  uid TEXT NOT NULL,
  etag TEXT,
  raw_ics TEXT NOT NULL,
  summary TEXT,
  description TEXT,
  location TEXT,
  dtstart TEXT,
  dtend TEXT,
  timezone TEXT,
  rrule TEXT,
  recurrence_id TEXT,
  status TEXT,
  organizer_json TEXT,
  attendees_json TEXT,
  deleted_at TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(calendar_id, href)
);
```

### `calendar_occurrences`

```sql
CREATE TABLE calendar_occurrences (
  id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL,
  occurrence_start TEXT NOT NULL,
  occurrence_end TEXT NOT NULL,
  recurrence_id TEXT,
  is_cancelled INTEGER DEFAULT 0
);
```

### `addressbooks`

```sql
CREATE TABLE addressbooks (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  url TEXT NOT NULL UNIQUE,
  display_name TEXT,
  sync_token TEXT,
  ctag TEXT,
  last_sync_at TEXT
);
```

### `contacts`

```sql
CREATE TABLE contacts (
  id TEXT PRIMARY KEY,
  addressbook_id TEXT NOT NULL,
  href TEXT NOT NULL,
  uid TEXT,
  etag TEXT,
  raw_vcard TEXT NOT NULL,
  display_name TEXT,
  given_name TEXT,
  family_name TEXT,
  emails_json TEXT,
  phones_json TEXT,
  organization TEXT,
  notes TEXT,
  deleted_at TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(addressbook_id, href)
);
```

### `person_aliases`

```sql
CREATE TABLE person_aliases (
  alias TEXT NOT NULL,
  normalized_alias TEXT NOT NULL,
  contact_id TEXT NOT NULL,
  alias_type TEXT NOT NULL,
  confidence REAL NOT NULL,
  PRIMARY KEY(normalized_alias, contact_id, alias_type)
);
```

### `search_documents`

```sql
CREATE TABLE search_documents (
  id TEXT PRIMARY KEY,
  domain TEXT NOT NULL,              -- mail | calendar | contact | mail_invite
  object_id TEXT NOT NULL,
  occurrence_id TEXT,
  title TEXT,
  canonical_text TEXT NOT NULL,
  metadata_json TEXT,
  updated_at TEXT NOT NULL,
  deleted_at TEXT
);
```

### `search_chunks`

```sql
CREATE TABLE search_chunks (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL,
  chunk_index INTEGER NOT NULL,
  text TEXT NOT NULL,
  token_count INTEGER,
  text_hash TEXT NOT NULL,
  embedding_model TEXT,
  embedding_status TEXT NOT NULL DEFAULT 'pending',
  metadata_json TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(document_id, chunk_index)
);
```

### `search_fts`

Use an external-content FTS5 table:

```sql
CREATE VIRTUAL TABLE search_fts USING fts5(
  title,
  text,
  sender,
  participants,
  domain,
  content='search_chunks',
  content_rowid='rowid',
  tokenize='unicode61 remove_diacritics 2'
);
```

For substring-heavy contact/name matching, add a separate trigram index for names/emails:

```sql
CREATE VIRTUAL TABLE contact_trigram_fts USING fts5(
  display_name,
  emails,
  tokenize='trigram'
);
```

FTS5 supports tokenizers including `unicode61`, `porter`, and `trigram`, and the trigram tokenizer can match arbitrary character sequences within a row rather than only complete tokens. ([SQLite][19])

---

# 7. Search and RAG architecture

## 7.1 Why hybrid search is required

Calendar/contact queries often need exact names, dates, and titles. Mail body queries often need semantic matching, paraphrase matching, and noisy text. Therefore:

| Search type        | Solves                                                             |
| ------------------ | ------------------------------------------------------------------ |
| Lexical FTS/BM25   | Exact names, email addresses, quoted phrases, dates, subject lines |
| Vector search      | Semantic phrasing: “appointment”, “meeting”, “sync”, “catch-up”    |
| Entity expansion   | “Liesa” → contact aliases/emails                                   |
| Time-aware ranking | “next meeting” → upcoming events                                   |
| Reranking          | Cross-domain result ordering                                       |

Hybrid search combines vector and full-text search. LanceDB’s docs describe hybrid search as combining vector and full-text techniques with reranking, and Qdrant describes dense embeddings for semantic search plus sparse/keyword signals for exact matching. ([LanceDB][20])

---

## 7.2 Query pipeline

For every `icloud.search` call:

```text
1. Normalize query
2. Resolve relative dates
3. Detect domain intent
4. Extract people/entities
5. Expand aliases from contacts
6. Run lexical FTS search
7. Run vector search
8. Run structured calendar/contact filters
9. Fuse results
10. Rerank
11. Generate compact snippets and answer_hints
12. Cache result
```

### Example: “What time is my meeting with Liesa?”

#### Step 1: query normalization

```json
{
  "raw": "What time is my meeting with Liesa?",
  "normalized": "meeting time liesa",
  "intent": "calendar_time_lookup",
  "entities": [{"type": "person", "text": "Liesa"}],
  "time_range": {
    "start": "now",
    "end": "now+90d"
  }
}
```

#### Step 2: alias expansion

```json
{
  "person": "Liesa",
  "expanded_terms": [
    "Liesa",
    "Liesa Müller",
    "liesa@example.com"
  ]
}
```

#### Step 3: search

Search these sources:

1. Calendar event title, description, attendees.
2. Calendar occurrence index for upcoming events.
3. Mail body chunks for emails that mention Liesa + meeting/time.
4. Contacts to confirm identity.

#### Step 4: answer hint

If top result is a calendar occurrence with high confidence:

```json
{
  "type": "calendar_time",
  "confidence": 0.91,
  "text": "Project Sync with Liesa is on 2026-04-27 from 14:00 to 15:00 Europe/Berlin.",
  "source_ids": ["cal_occ_123"]
}
```

The MCP server should not fabricate the final answer. It should return evidence and deterministic answer hints; the host model can then produce the user-facing response.

---

## 7.3 Search document construction

### Mail document

```text
Title: Re: Project Sync
People: From Liesa Müller <liesa@example.com>, To me
Date: 2026-04-21 11:23 Europe/Berlin
Mailbox: INBOX
Body:
Let's meet Monday at 14:00...
```

Chunks:

| Chunk        | Text                              |
| ------------ | --------------------------------- |
| Header chunk | subject, sender, recipients, date |
| Body chunk 1 | first 800–1200 tokens             |
| Body chunk N | next segment                      |
| Invite chunk | parsed ICS details, if present    |

### Calendar document

```text
Title: Project Sync with Liesa
Start: 2026-04-27 14:00 Europe/Berlin
End: 2026-04-27 15:00 Europe/Berlin
Attendees: Liesa Müller <liesa@example.com>
Location: Zoom
Description: Discuss project timeline
```

Create both:

1. One document for the event master.
2. One lightweight document per occurrence in the searchable window.

### Contact document

```text
Name: Liesa Müller
Emails: liesa@example.com
Organization: Example GmbH
Phones: ...
Notes: optional, depending on privacy config
```

---

## 7.4 Ranking formula

Use Reciprocal Rank Fusion-style combination plus domain-specific boosts:

```text
score =
  0.35 * lexical_rank_score
+ 0.25 * vector_rank_score
+ 0.15 * entity_match_score
+ 0.10 * time_relevance_score
+ 0.05 * domain_intent_score
+ 0.05 * freshness_score
+ 0.05 * source_quality_score
```

Suggested boosts:

| Signal                                          | Boost |
| ----------------------------------------------- | ----: |
| Exact contact alias match                       | +0.15 |
| Exact email match                               | +0.20 |
| Calendar query and result is upcoming event     | +0.15 |
| Query says “what time” and result has start/end | +0.15 |
| Mail body match only                            | +0.05 |
| Mail subject match                              | +0.10 |
| Event attendee match                            | +0.20 |
| Cancelled event                                 | −0.50 |
| Spam folder                                     | −0.30 |
| Very old result without explicit past intent    | −0.20 |

---

## 7.5 Result caching

### Cache layers

| Layer                   | Storage                            | Purpose                                |
| ----------------------- | ---------------------------------- | -------------------------------------- |
| Process LRU             | In-memory                          | Hot object/view/search responses       |
| Persistent object cache | SQLite/Postgres                    | Source of truth for synced iCloud data |
| FTS index               | SQLite FTS5/Postgres FTS           | Fast exact search                      |
| Vector index            | LanceDB/sqlite-vec/Qdrant/pgvector | Semantic search                        |
| Query cache             | SQLite or Redis                    | Normalized query result reuse          |
| Connection pool         | IMAP/DAV clients                   | Avoid repeated setup overhead          |

Redis is appropriate for HTTP or multi-process deployments because it is commonly used as a cache, supports memory limits, and can evict keys using configured eviction policies. ([Redis][21])

### Query cache key

```text
sha256(
  normalized_query
  + sorted_domains
  + normalized_time_range
  + person_filter
  + limit
  + index_generation
  + user_id
)
```

### Invalidation

Increment `index_generation` when:

| Event                           | Invalidate                      |
| ------------------------------- | ------------------------------- |
| New/updated mail chunk          | Mail query cache                |
| Calendar event changed          | Calendar + unified search cache |
| Contact changed                 | Contact + unified search cache  |
| Embedding model changed         | All semantic caches             |
| Sync detects UIDVALIDITY reset  | Mail cache for mailbox          |
| CalDAV/CardDAV sync token reset | Affected collection cache       |

### TTLs

| Cache                         |                         TTL |
| ----------------------------- | --------------------------: |
| Hot search result             |                5–30 minutes |
| Object view cache             |              30–120 minutes |
| Contact alias cache           |  Until contact sync changes |
| Calendar upcoming event cache | Until calendar sync changes |
| Mail body chunk cache         |  Until message hash changes |

---

# 8. Sync and indexing strategy

## 8.1 Startup sequence

On server start:

```text
1. Load config and secrets
2. Open database
3. Run migrations
4. Start FastMCP server
5. Start background scheduler
6. Sync contacts first
7. Sync calendars second
8. Sync recent mail headers
9. Sync recent mail bodies
10. Backfill old mail gradually
```

Contacts should sync before search because contacts improve entity expansion. Calendar should sync before full mail body backfill because calendar queries are common and small. Mail body backfill is the most expensive.

## 8.2 Sync priorities

| Priority | Data                              |
| -------- | --------------------------------- |
| P0       | Contacts aliases                  |
| P0       | Calendars and next 90 days        |
| P1       | Recent mail headers, last 30 days |
| P1       | Recent mail bodies, last 30 days  |
| P2       | Calendar past/future expansion    |
| P2       | Older mail body backfill          |
| P3       | Attachment indexing, if enabled   |

## 8.3 Incremental indexer

For every changed object:

```text
remote object changed
→ upsert raw object
→ normalize object
→ compute text hash
→ if text hash changed: update chunks
→ FTS index update
→ embedding job enqueue
→ bump index_generation
```

Embedding should be asynchronous and batched. The search tool should still work with lexical FTS while embeddings are pending.

## 8.4 Freshness policy

Expose freshness in every search result:

```json
{
  "index_freshness": {
    "calendar_last_sync": "2026-04-24T09:03:00+02:00",
    "mail_last_sync": "2026-04-24T09:01:00+02:00",
    "contacts_last_sync": "2026-04-24T08:58:00+02:00",
    "mail_backfill_status": "recent_complete_older_pending"
  }
}
```

Do not silently pretend stale indexes are complete.

---

# 9. Token efficiency design

## 9.1 Default outputs must be compact

List/search tools should never return full bodies by default.

Return:

```text
id
domain
title
time/date
people
short snippet
score
why
```

Then the model can call `mail.view` or `calendar.view_event` only for the selected item.

## 9.2 Use structured output

FastMCP supports structured tool output and `ToolResult` metadata, which is useful for returning compact content plus machine-readable data and timing/debug metadata. ([FastMCP][4])

Recommended response pattern:

```json
{
  "content": "Found 3 likely matches.",
  "structured_content": {
    "results": [...]
  },
  "meta": {
    "execution_time_ms": 42,
    "cache": "hit",
    "index_generation": 1042
  }
}
```

## 9.3 Snippet policy

For each search result:

| Field                      |                            Max |
| -------------------------- | -----------------------------: |
| title                      |                      120 chars |
| snippet                    |                  300–500 chars |
| people                     |                       5 people |
| results                    |                     default 10 |
| mail body returned by view |            default 8,000 chars |
| full raw ICS/vCard         | only when explicitly requested |

## 9.4 Cursor pagination

All list/search tools must support `limit` and `cursor`.

Cursor content:

```json
{
  "offset": 25,
  "query_hash": "...",
  "index_generation": 1042,
  "expires_at": "2026-04-24T10:00:00+02:00"
}
```

Encode as base64url JSON with HMAC to prevent tampering.

---

# 10. Security design

## 10.1 Credential handling

Rules:

1. Never accept Apple credentials as normal MCP tool arguments.
2. Configure credentials out-of-band through environment variables, a config file, or OS keychain.
3. Prefer OS keychain for local deployments.
4. Never log app-specific passwords.
5. Redact emails and message bodies in debug logs unless explicitly enabled.
6. Support password rotation.
7. For HTTP deployment, never share one iCloud credential across users.

Apple app-specific passwords can be revoked individually or all at once, and primary password changes revoke existing app-specific passwords, so the implementation must detect auth failures and surface a clear “credential revoked or expired” status. ([Apple Support][2])

## 10.2 MCP authorization

For local STDIO, credentials can be environment/keychain-based. For HTTP, require auth. MCP’s security guidance recommends authorization when a server accesses user-specific data such as emails or documents, and MCP’s authorization flow uses OAuth-style bearer tokens for protected MCP servers. ([Model Context Protocol][22])

Required HTTP controls:

| Control                    | Requirement |
| -------------------------- | ----------- |
| TLS                        | Required    |
| Token audience validation  | Required    |
| Per-user account isolation | Required    |
| Scope checks               | Required    |
| Audit logs                 | Required    |
| Rate limits                | Required    |
| No token passthrough       | Required    |

## 10.3 Tool annotations

Read tools:

```json
{
  "readOnlyHint": true,
  "idempotentHint": true,
  "openWorldHint": false
}
```

Calendar create/update:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": true
}
```

Annotations improve client UX but are not a security boundary; FastMCP explicitly describes them as advisory hints, so server-side validation and authorization are still required. ([FastMCP][4])

## 10.4 Prompt injection and hostile content

Mail bodies, calendar descriptions, contact notes, and invite text are untrusted data. OWASP’s MCP Top 10 calls out token/secret exposure, privilege escalation, tool poisoning, command injection, insufficient auth, and prompt/context injection as MCP risks. ([owasp.org][23])

Mitigations:

| Risk                                     | Mitigation                                             |
| ---------------------------------------- | ------------------------------------------------------ |
| Mail says “ignore previous instructions” | Return as quoted data with source labels               |
| Tool output poisoning                    | Never include hidden instructions in tool descriptions |
| Secret leakage                           | Redact config/secrets from all outputs                 |
| Over-sharing                             | Return minimum snippets; view requires explicit ID     |
| Calendar write abuse                     | Validate fields and require update target              |
| Command injection                        | No shell commands from user/retrieved text             |
| Dependency tampering                     | Pin deps, lockfile, SBOM, CI security checks           |

## 10.5 Calendar write guardrails

For create/update:

1. Validate title length.
2. Validate start/end timezone.
3. Reject end before start.
4. Reject enormous recurrence rules.
5. Validate attendees are emails.
6. Preserve unknown ICS fields.
7. Use ETag conflict handling.
8. Log write audit event without body secrets.
9. Return a diff summary.

No delete tool should be included in this version.

---

# 11. Reliability and performance requirements

## 11.1 Latency targets

| Operation                          |                    Target |
| ---------------------------------- | ------------------------: |
| Hot cached search                  |                   <100 ms |
| Local hybrid search                |                   <300 ms |
| Mail/calendar/contact view from DB |                   <100 ms |
| View requiring remote lazy fetch   |                    <2–5 s |
| Calendar create/update             | <1–3 s, network-dependent |
| Background sync                    |  Does not block MCP calls |

## 11.2 Background workers

Use separate workers for:

```text
mail_sync_worker
calendar_sync_worker
contacts_sync_worker
indexer_worker
embedding_worker
maintenance_worker
```

Each worker must have:

| Feature                         | Purpose                       |
| ------------------------------- | ----------------------------- |
| Exponential backoff with jitter | iCloud/network resilience     |
| Per-domain circuit breaker      | Avoid repeated failures       |
| Dead-letter queue               | Preserve failed indexing jobs |
| Locking                         | Prevent duplicate syncs       |
| Checkpointing                   | Resume after crash            |

## 11.3 Offline behavior

If iCloud is unreachable:

* `search`, `list`, and `view` still work from local cache.
* Results include stale freshness metadata.
* Calendar writes return a clear connectivity error and do not queue by default unless explicitly configured.

## 11.4 Idempotency

For calendar create:

```json
{
  "request_id": "user-or-client-generated-id"
}
```

Store `request_id → event_id` so retries do not create duplicates.

For calendar update:

* Use ETag where possible.
* Return conflict if remote changed.

---

# 12. Implementation stack

## 12.1 Core dependencies

```toml
[project]
requires-python = ">=3.11"
dependencies = [
  "fastmcp",
  "pydantic",
  "pydantic-settings",
  "imapclient",
  "caldav",
  "httpx",
  "vobject",
  "icalendar",
  "python-dateutil",
  "beautifulsoup4",
  "lxml",
  "defusedxml",
  "aiosqlite",
  "keyring",
  "orjson",
  "tenacity"
]
```

`aiosqlite` provides an async interface to SQLite and mirrors standard sqlite3 methods with async versions, which fits FastMCP async tools and background workers. ([aiosqlite.omnilib.dev][24])

## 12.2 Search dependencies

Choose one local vector option:

### Option A: SQLite-only

```toml
"sqlite-vec"
```

Best for minimal deployment. `sqlite-vec` is an embedded SQLite vector search extension, but it is still pre-v1 according to its project page, so pin versions carefully. ([GitHub][25])

### Option B: SQLite + LanceDB

```toml
"lancedb"
```

Best current balance for local hybrid search because LanceDB supports vector search, full-text search, hybrid search, and reranking patterns. ([LanceDB][20])

### Option C: Qdrant

```toml
"qdrant-client[fastembed]"
```

Best for larger or remote deployments. Qdrant is a Rust-based vector search engine with filtering and hybrid dense/sparse retrieval support. ([Qdrant][26])

### Recommended

Use **SQLite + FTS5 + LanceDB** for the first production-quality version. Keep an abstraction so Qdrant or Postgres/pgvector can replace the vector backend later.

---

# 13. Project structure

```text
icloud_mcp/
  pyproject.toml
  README.md
  src/icloud_mcp/
    server.py
    config.py
    security/
      secrets.py
      redaction.py
      auth.py
    db/
      connection.py
      migrations/
      models.py
      repositories.py
    adapters/
      imap_mail.py
      caldav_calendar.py
      carddav_contacts.py
      dav_xml.py
    sync/
      scheduler.py
      mail_sync.py
      calendar_sync.py
      contacts_sync.py
      checkpoints.py
    indexing/
      normalizers.py
      chunker.py
      fts.py
      vector.py
      embeddings.py
      rerank.py
      query_planner.py
    tools/
      search_tools.py
      mail_tools.py
      contact_tools.py
      calendar_tools.py
      sync_tools.py
    schemas/
      search.py
      mail.py
      contacts.py
      calendar.py
    observability/
      logging.py
      metrics.py
      audit.py
  tests/
    unit/
    integration/
    fixtures/
      imap/
      caldav/
      carddav/
```

---

# 14. FastMCP server skeleton

```python
from fastmcp import FastMCP
from icloud_mcp.tools.search_tools import register_search_tools
from icloud_mcp.tools.mail_tools import register_mail_tools
from icloud_mcp.tools.contact_tools import register_contact_tools
from icloud_mcp.tools.calendar_tools import register_calendar_tools
from icloud_mcp.config import Settings
from icloud_mcp.db.connection import open_db
from icloud_mcp.sync.scheduler import SyncScheduler

settings = Settings()

mcp = FastMCP(
    name="iCloud MCP",
    instructions=(
        "Use this server to search and view the user's iCloud Mail, "
        "Calendar, and Contacts. Most tools are read-only. Only calendar "
        "create/update tools can modify iCloud Calendar."
    ),
    version="0.1.0",
    mask_error_details=True,
)

db = open_db(settings.database_url)

register_search_tools(mcp, db, settings)
register_mail_tools(mcp, db, settings)
register_contact_tools(mcp, db, settings)
register_calendar_tools(mcp, db, settings)

scheduler = SyncScheduler(db=db, settings=settings)

if __name__ == "__main__":
    scheduler.start_background()
    if settings.transport == "http":
        mcp.run(transport="http", host=settings.host, port=settings.port)
    else:
        mcp.run()
```

FastMCP supports constructor-level settings such as server identity, tools, auth, middleware, and behavior, and it can run with STDIO by default or HTTP transport when configured. ([FastMCP][27])

---

# 15. Testing strategy

## 15.1 Unit tests

Test:

| Area            | Tests                                          |
| --------------- | ---------------------------------------------- |
| Query planner   | names, dates, “what time”, “next”, “last”      |
| Alias expansion | contact names, nicknames, emails               |
| Mail parser     | plain text, HTML, multipart, quoted replies    |
| ICS parser      | single event, recurring event, cancelled event |
| vCard parser    | names, emails, phones, org                     |
| Ranking         | exact vs semantic, upcoming vs old             |
| Token limits    | output truncation and cursors                  |

## 15.2 Integration tests

Use fake protocol servers:

| Protocol | Test fixture                         |
| -------- | ------------------------------------ |
| IMAP     | GreenMail/Docker or Python fake IMAP |
| CalDAV   | Radicale test server                 |
| CardDAV  | Radicale test server                 |

Test flows:

1. Initial sync.
2. Incremental sync.
3. Message body search.
4. Contact alias search.
5. Calendar recurrence search.
6. Calendar create.
7. Calendar update with ETag conflict.
8. Offline search from cache.
9. Credential failure.
10. Stale index metadata.

## 15.3 Golden prompt tests

Use fixed prompts:

```text
What time is my meeting with Liesa?
Show emails from Liesa last week.
Find the email where someone mentioned the contract deadline.
Who is Liesa?
What meetings do I have tomorrow?
Create a calendar event tomorrow at 10 called Focus Time.
Move my Project Sync with Liesa to 15:00.
```

Expected behavior:

* Search chooses correct domain.
* Results include source IDs.
* Calendar writes require structured fields.
* Mail body-only matches work.
* Ambiguous results return candidates, not fabricated answers.

---

# 16. Implementation phases

## Phase 1 — Core MCP + local DB

Deliver:

* FastMCP server scaffold.
* Config/secrets.
* SQLite WAL schema.
* Basic tool registration.
* `sync.status`.

## Phase 2 — Calendar MVP

Deliver:

* CalDAV discovery.
* Calendar list.
* Event list/view/search.
* Create event.
* Update non-recurring event.
* ETag conflict handling.

## Phase 3 — Contacts MVP

Deliver:

* CardDAV discovery.
* Contact list/view/search.
* Alias table.
* Contact-aware calendar search.

## Phase 4 — Mail MVP

Deliver:

* IMAP mailbox discovery.
* Recent header sync.
* Recent body sync.
* Mail list/view/search.
* HTML-to-text parsing.

## Phase 5 — Unified RAG search

Deliver:

* FTS5 index.
* Vector backend abstraction.
* Embedding worker.
* Hybrid search.
* Reranking.
* Query cache.
* `answer_hints`.

## Phase 6 — Reliability hardening

Deliver:

* Backoff/circuit breakers.
* Incremental sync checkpoints.
* Offline mode.
* Audit logs.
* Token-budget enforcement.
* Conflict and stale-index reporting.

## Phase 7 — Production hardening

Deliver:

* HTTP transport option.
* Token verification/OAuth for HTTP.
* Per-user isolation.
* Redis cache option.
* Metrics endpoint.
* Dependency pinning and SBOM.
* Security test suite.

---

# 17. Key design decisions

| Decision              | Recommendation                                             |
| --------------------- | ---------------------------------------------------------- |
| iCloud API approach   | Use IMAP, CalDAV, CardDAV                                  |
| Search strategy       | Local hybrid search, not direct iCloud search              |
| RAG store             | SQLite FTS5 + LanceDB initially                            |
| Query cache           | Normalized query + index generation                        |
| Mail sync             | Recent-first, body backfill in background                  |
| Calendar write safety | ETag, validation, no delete tool                           |
| Contact handling      | Alias expansion for people queries                         |
| Deployment            | Local STDIO by default                                     |
| Remote deployment     | HTTP only with token verification/OAuth                    |
| Token efficiency      | IDs + snippets first, explicit view tools for full content |

This design gives you a fast MCP server because searches run locally, reliable behavior because remote iCloud sync is isolated from interactive tool latency, and good answer quality because exact search, semantic search, contact aliases, calendar occurrences, and mail body chunks all participate in one retrieval pipeline.

[1]: https://support.apple.com/en-us/102525 "iCloud Mail server settings for other email client apps - Apple Support"
[2]: https://support.apple.com/en-us/102654 "Sign in to apps with your Apple Account using app-specific passwords - Apple Support"
[3]: https://support.apple.com/en-us/102651 "iCloud data security overview - Apple Support"
[4]: https://gofastmcp.com/servers/tools "Tools - FastMCP"
[5]: https://modelcontextprotocol.io/specification/2025-11-25 "Specification - Model Context Protocol"
[6]: https://gofastmcp.com/deployment/running-server?utm_source=chatgpt.com "Running Your Server"
[7]: https://gofastmcp.com/deployment/http "HTTP Deployment - FastMCP"
[8]: https://gofastmcp.com/servers/auth/token-verification "Token Verification - FastMCP"
[9]: https://imapclient.readthedocs.io/en/3.0.1/api.html?utm_source=chatgpt.com "IMAPClient 3.0.1 documentation"
[10]: https://caldav.readthedocs.io/latest/about.html "About the Python CalDAV Client Library — caldav 3.1.1.dev14+gf76d107d6 documentation"
[11]: https://datatracker.ietf.org/doc/html/rfc4791?utm_source=chatgpt.com "RFC 4791 - Calendaring Extensions to WebDAV (CalDAV)"
[12]: https://pypi.org/project/icalendar/?utm_source=chatgpt.com "icalendar"
[13]: https://datatracker.ietf.org/doc/html/rfc6578?utm_source=chatgpt.com "RFC 6578 - Collection Synchronization for Web Distributed ..."
[14]: https://support.apple.com/en-gb/guide/deployment/depd2181f425/web "Contacts declarative configuration for Apple devices – Apple Support (UK)"
[15]: https://discourse.gnome.org/t/gnome-settings-carddav-caldav-icloud-account/22859?utm_source=chatgpt.com "Gnome settings => CardDAV/CalDAV iCloud account"
[16]: https://datatracker.ietf.org/doc/html/rfc6352?utm_source=chatgpt.com "RFC 6352 - CardDAV: vCard Extensions to Web Distributed ..."
[17]: https://www.sqlite.org/fts5.html?utm_source=chatgpt.com "SQLite FTS5 Extension"
[18]: https://www.sqlite.org/wal.html "Write-Ahead Logging"
[19]: https://www.sqlite.org/fts5.html "SQLite FTS5 Extension"
[20]: https://docs.lancedb.com/search/hybrid-search?utm_source=chatgpt.com "Hybrid Search"
[21]: https://redis.io/docs/latest/develop/reference/eviction/ "Key eviction | Docs"
[22]: https://modelcontextprotocol.io/docs/tutorials/security/authorization "Understanding Authorization in MCP - Model Context Protocol"
[23]: https://owasp.org/www-project-mcp-top-10/ "OWASP MCP Top 10 | OWASP Foundation"
[24]: https://aiosqlite.omnilib.dev/?utm_source=chatgpt.com "aiosqlite: Sqlite for AsyncIO — aiosqlite documentation"
[25]: https://github.com/asg017/sqlite-vec?utm_source=chatgpt.com "asg017/sqlite-vec: A vector search ..."
[26]: https://qdrant.tech/?utm_source=chatgpt.com "Qdrant - Vector Search Engine"
[27]: https://gofastmcp.com/servers/server "The FastMCP Server - FastMCP"
