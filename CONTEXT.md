# iCloud MCP Server

Local-first cache for iCloud Mail, Calendar, and Contacts exposed through MCP tools.

## Language

**Local Cache**:
SQLite copy of iCloud data used to answer MCP reads without network access.
_Avoid_: database, store

**Search Index**:
Derived searchable documents, chunks, and ranking state built from cached Mail, Calendar, and Contact records.
_Avoid_: FTS tables, vectors

**Mail Cache**:
Cached iCloud Mail mailboxes, messages, and invite evidence.
_Avoid_: email store

**Calendar Cache**:
Cached iCloud Calendar collections, events, recurrence windows, and write metadata.
_Avoid_: event store

**Contact Cache**:
Cached iCloud Contacts addressbooks, contacts, and person aliases.
_Avoid_: people store

**Cache Maintenance**:
Cleanup that removes stale cached rows and derived index state after sync changes.
_Avoid_: housekeeping

## Relationships

- A **Local Cache** contains one **Mail Cache**, one **Calendar Cache**, and one **Contact Cache**.
- A **Search Index** is derived from the **Mail Cache**, **Calendar Cache**, and **Contact Cache**.
- **Cache Maintenance** repairs the **Local Cache** and **Search Index** after sync.

## Example dialogue

> **Dev:** "Should the Mail list tool query iCloud when a message is missing?"
> **Domain expert:** "No. MCP reads use the **Local Cache**; sync workers refresh the **Mail Cache**, then the **Search Index** is rebuilt from cached data."

## Flagged ambiguities

- "repository" was used for both all cached domains and individual cache areas; resolved: each cache area owns its repository Module, and **Search Index** owns search-specific indexing behavior.
