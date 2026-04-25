"""SQLite connection and schema management."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from threading import RLock
from typing import Any

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS accounts (
  id TEXT PRIMARY KEY,
  apple_id_hash TEXT NOT NULL,
  display_name TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mailboxes (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  name TEXT NOT NULL,
  uid_validity TEXT,
  uid_next INTEGER,
  highest_modseq TEXT,
  last_synced_uid INTEGER,
  folder_quality TEXT DEFAULT 'normal',
  backfill_cursor TEXT,
  backfill_status TEXT,
  last_sync_at TEXT,
  UNIQUE(account_id, name)
);

CREATE TABLE IF NOT EXISTS mail_messages (
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
  bcc_json TEXT,
  in_reply_to TEXT,
  references_json TEXT,
  date TEXT,
  flags_json TEXT,
  size_bytes INTEGER,
  preview TEXT,
  body_text TEXT,
  body_hash TEXT,
  body_unavailable_reason TEXT,
  body_indexed_chars INTEGER DEFAULT 0,
  has_attachments INTEGER DEFAULT 0,
  attachments_json TEXT,
  deleted_at TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(mailbox_id, uid)
);

CREATE TABLE IF NOT EXISTS calendar_collections (
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

CREATE TABLE IF NOT EXISTS calendar_objects (
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

CREATE TABLE IF NOT EXISTS calendar_occurrences (
  id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL,
  occurrence_start TEXT NOT NULL,
  occurrence_end TEXT NOT NULL,
  recurrence_id TEXT,
  is_cancelled INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS addressbooks (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  url TEXT NOT NULL UNIQUE,
  display_name TEXT,
  sync_token TEXT,
  ctag TEXT,
  last_sync_at TEXT
);

CREATE TABLE IF NOT EXISTS contacts (
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

CREATE TABLE IF NOT EXISTS person_aliases (
  alias TEXT NOT NULL,
  normalized_alias TEXT NOT NULL,
  contact_id TEXT NOT NULL,
  alias_type TEXT NOT NULL,
  confidence REAL NOT NULL,
  PRIMARY KEY(normalized_alias, contact_id, alias_type)
);

CREATE TABLE IF NOT EXISTS search_documents (
  id TEXT PRIMARY KEY,
  domain TEXT NOT NULL,
  object_id TEXT NOT NULL,
  occurrence_id TEXT,
  title TEXT,
  canonical_text TEXT NOT NULL,
  metadata_json TEXT,
  updated_at TEXT NOT NULL,
  deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS search_chunks (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL,
  chunk_index INTEGER NOT NULL,
  chunk_type TEXT DEFAULT 'body',
  text TEXT NOT NULL,
  token_count INTEGER,
  text_hash TEXT NOT NULL,
  embedding_model TEXT,
  embedding_status TEXT NOT NULL DEFAULT 'pending',
  metadata_json TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(document_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS search_embeddings (
  chunk_id TEXT PRIMARY KEY,
  embedding_model TEXT NOT NULL,
  vector_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vector_backend_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  backend TEXT NOT NULL,
  dimensions INTEGER NOT NULL,
  available INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
  document_id UNINDEXED,
  object_id UNINDEXED,
  domain,
  title,
  text,
  sender,
  participants,
  tokenize='unicode61 remove_diacritics 2'
);

CREATE VIRTUAL TABLE IF NOT EXISTS contact_trigram_fts USING fts5(
  contact_id UNINDEXED,
  display_name,
  emails,
  tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS sync_checkpoints (
  name TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  last_sync_at TEXT,
  last_error TEXT,
  retry_count INTEGER DEFAULT 0,
  backoff_until TEXT,
  progress_cursor TEXT,
  detail_json TEXT
);

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metrics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  value REAL NOT NULL,
  tags_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS query_cache (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  index_generation INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS index_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  generation INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
  request_id TEXT PRIMARY KEY,
  operation TEXT NOT NULL,
  object_id TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
  id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  object_id TEXT,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL
);

"""

SCHEMA_VERSION = 2


class Database:
    """Small sqlite3 wrapper with dict rows."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self._lock = RLock()

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self.connection.execute(sql, parameters)
            self.connection.commit()
            return cursor

    def executemany(self, sql: str, parameters: list[tuple[Any, ...]]) -> None:
        with self._lock:
            self.connection.executemany(sql, parameters)
            self.connection.commit()

    def query(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self.connection.execute(sql, parameters).fetchall()]

    def query_one(self, sql: str, parameters: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute(sql, parameters).fetchone()
            return dict(row) if row else None

    def executescript(self, script: str) -> None:
        with self._lock:
            self.connection.executescript(script)
            self.connection.commit()

    def close(self) -> None:
        with self._lock:
            self.connection.close()


def open_db(path: str | Path) -> Database:
    """Open SQLite database, run schema, and ensure local default collections."""

    database_path = Path(path)
    if str(database_path) != ":memory:":
        database_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(database_path, check_same_thread=False)
    db = Database(connection)
    db.executescript(SCHEMA)
    run_migrations(db)
    return db


def run_migrations(db: Database) -> None:
    """Apply additive migrations for existing local SQLite caches."""

    _ensure_column(db, "mailboxes", "last_synced_uid", "INTEGER")
    _ensure_column(db, "mailboxes", "folder_quality", "TEXT DEFAULT 'normal'")
    _ensure_column(db, "mailboxes", "backfill_cursor", "TEXT")
    _ensure_column(db, "mailboxes", "backfill_status", "TEXT")
    _ensure_column(db, "mail_messages", "bcc_json", "TEXT")
    _ensure_column(db, "mail_messages", "in_reply_to", "TEXT")
    _ensure_column(db, "mail_messages", "references_json", "TEXT")
    _ensure_column(db, "mail_messages", "body_unavailable_reason", "TEXT")
    _ensure_column(db, "mail_messages", "body_indexed_chars", "INTEGER DEFAULT 0")
    _ensure_column(db, "mail_messages", "attachments_json", "TEXT")
    _ensure_column(db, "search_chunks", "chunk_type", "TEXT DEFAULT 'body'")
    _ensure_column(db, "sync_checkpoints", "last_error", "TEXT")
    _ensure_column(db, "sync_checkpoints", "retry_count", "INTEGER DEFAULT 0")
    _ensure_column(db, "sync_checkpoints", "backoff_until", "TEXT")
    _ensure_column(db, "sync_checkpoints", "progress_cursor", "TEXT")
    _ensure_indexes(db)
    db.execute(
        """
        INSERT OR IGNORE INTO schema_migrations (version, applied_at)
        VALUES (?, datetime('now'))
        """,
        (SCHEMA_VERSION,),
    )


def _ensure_column(db: Database, table: str, column: str, definition: str) -> None:
    existing = {row["name"] for row in db.query(f"PRAGMA table_info({table})")}
    if column in existing:
        return
    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _ensure_indexes(db: Database) -> None:
    indexes = [
        (
            "mail_messages",
            {"mailbox_id", "deleted_at", "date"},
            "CREATE INDEX IF NOT EXISTS idx_mail_messages_mailbox_deleted_date "
            "ON mail_messages(mailbox_id, deleted_at, date DESC)",
        ),
        (
            "calendar_occurrences",
            {"occurrence_start", "occurrence_end", "event_id"},
            "CREATE INDEX IF NOT EXISTS idx_calendar_occurrences_start_end_event "
            "ON calendar_occurrences(occurrence_start, occurrence_end, event_id)",
        ),
        (
            "calendar_objects",
            {"calendar_id", "deleted_at"},
            "CREATE INDEX IF NOT EXISTS idx_calendar_objects_calendar_deleted "
            "ON calendar_objects(calendar_id, deleted_at)",
        ),
        (
            "search_documents",
            {"domain", "deleted_at", "object_id"},
            "CREATE INDEX IF NOT EXISTS idx_search_documents_domain_deleted_object "
            "ON search_documents(domain, deleted_at, object_id)",
        ),
        (
            "contacts",
            {"addressbook_id", "deleted_at", "display_name"},
            "CREATE INDEX IF NOT EXISTS idx_contacts_addressbook_deleted_display "
            "ON contacts(addressbook_id, deleted_at, display_name)",
        ),
        (
            "person_aliases",
            {"contact_id", "confidence"},
            "CREATE INDEX IF NOT EXISTS idx_person_aliases_contact_confidence "
            "ON person_aliases(contact_id, confidence DESC)",
        ),
        (
            "query_cache",
            {"expires_at", "index_generation"},
            "CREATE INDEX IF NOT EXISTS idx_query_cache_expires_generation "
            "ON query_cache(expires_at, index_generation)",
        ),
    ]
    for table, required_columns, sql in indexes:
        columns = {row["name"] for row in db.query(f"PRAGMA table_info({table})")}
        if required_columns <= columns:
            db.execute(sql)
