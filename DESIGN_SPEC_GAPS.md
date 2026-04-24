# Design Spec Gaps

Source of truth: `docs/design/design.md`

Audit date: 2026-04-24

Scope: this file lists requirements from the Design Spec that are not implemented or only partially implemented in the current codebase. It does not repeat requirements that are already covered end to end.

## Summary

The project implements the local-first FastMCP scaffold, SQLite schema, core tools, protocol adapters, sync workers, FTS search, hashed semantic scoring, cursor signing, calendar write guardrails, and basic audit logging.

The remaining work is concentrated in six areas:

1. Incremental sync correctness for IMAP, CalDAV, and CardDAV.
2. Full calendar recurrence safety, including exceptions and partial-series updates.
3. Mail indexing completeness, especially invites, attachments, huge/encrypted messages, and reply-chain handling.
4. Full hybrid RAG behavior, including query planning, automatic entity extraction, reranking, durable embeddings, per-occurrence documents, and richer answer hints.
5. Reliability and security hardening, including retries, circuit breakers, stale/credential status detail, keychain support, and prompt-injection handling.
6. Test coverage beyond local unit tests, especially fake protocol integration tests, golden prompt tests, and security tests.

## 1. Architecture And Deployment

### Optional MCP Resources And Prompts

Status: not implemented.

Design Spec reference: section 1 says MCP models server capabilities as Tools, Resources, and Prompts, and says the server should expose compact tools plus optional resources for object retrieval by URI.

Current state:

- Only FastMCP tools are registered in `src/icloud_mcp/server.py`.
- No MCP resources are registered for `mail://`, `calendar://`, `contact://`, or similar object retrieval.
- No MCP prompts are registered.

Remaining work:

- Add resource URI patterns for cached mail messages, calendar events, and contacts.
- Keep view tools as the compact default, but expose resources for clients that prefer object retrieval.
- Add prompts only if there is a concrete client workflow that benefits from them.

### Service Layer Boundary

Status: partial.

Design Spec reference: section 1 describes `FastMCP tools -> service layer -> local cache + hybrid search index -> background iCloud sync workers -> IMAP / CalDAV / CardDAV`.

Current state:

- Tool handlers call repository functions directly.
- Protocol sync workers are separated from interactive tools.
- There is no explicit service layer coordinating validation, cache policy, search planning, and repository calls.

Remaining work:

- Add a narrow service layer only where it reduces duplication or makes policy explicit.
- Candidate services: `SearchService`, `CalendarWriteService`, and `SyncStatusService`.

## 2. Mail Adapter And Mail Indexing

### Incremental IMAP Sync

Status: partial.

Design Spec reference: section 4.1 sync algorithm requires storing mailbox sync state, discovering mailboxes, fetching changes incrementally, and tracking metadata such as `uid_validity`, `uid_next`, `highest_modseq`, and last synced UID.

Current state:

- Mailbox discovery and recent message fetch exist in `src/icloud_mcp/adapters/imap_mail.py`.
- `uid_validity`, `uid_next`, and `highest_modseq` are stored in `mailboxes`.
- The sync worker fetches recent messages by date window each run.
- No explicit last synced UID is stored.
- No CONDSTORE/QRESYNC or MODSEQ-based delta fetch is implemented.
- No deletion, expunge, move, or flag-only update reconciliation is implemented.

Remaining work:

- Store `last_synced_uid` or equivalent per mailbox.
- Use `UIDVALIDITY` to detect mailbox reset.
- Use `HIGHESTMODSEQ` when the server supports it.
- Reconcile deleted or moved messages by marking `mail_messages.deleted_at`.
- Update flags without requiring body refetch when possible.

### Mail Header Completeness

Status: partial.

Design Spec reference: section 4.1 requires storing UID, Message-ID, In-Reply-To, References, Date, From, To, Cc, Bcc, Subject, Flags, and Size.

Current state:

- UID, Message-ID, Date, From, To, Cc, Subject, Flags, and Size are handled.
- Bcc is not stored.
- In-Reply-To and References are not stored.
- `thread_id` exists in the schema but is not populated.

Remaining work:

- Add Bcc, In-Reply-To, and References extraction.
- Populate `thread_id` from Message-ID threading headers or a deterministic local thread key.
- Add tests for threaded messages and Bcc parsing.

### `text/calendar` Invite Evidence

Status: partial.

Design Spec reference: sections 4.1 and 7.3 require parsing `text/calendar` invite parts and indexing them as `mail_invite` evidence.

Current state:

- `text/calendar` MIME parts are included in body text by `_append_part_text`.
- No parsed invite document is created.
- No `mail_invite` search document domain is used.
- Invite fields such as organizer, attendees, proposed time, UID, and method are not normalized.

Remaining work:

- Parse `text/calendar` parts with `icalendar`.
- Create separate `mail_invite` search documents linked to the source mail.
- Include invite time, organizer, attendees, status/method, and source mail ID in metadata.
- Add search tests where an invite exists in mail but not on the calendar.

### Attachment Metadata And Optional Attachment Text Indexing

Status: not implemented.

Design Spec reference: section 4.1 says attachment metadata should be indexed by default and small text/PDF attachments may be indexed behind a config flag.

Current state:

- `has_attachments` is detected.
- `mail.view(... include=["attachments"])` always returns an empty list.
- No attachment table or attachment metadata JSON exists.
- No config flag controls attachment text indexing.

Remaining work:

- Store attachment filename, MIME type, size, content ID, and disposition metadata.
- Return metadata from `mail.view` when attachments are requested.
- Add optional text/PDF extraction behind a disabled-by-default setting.

### Huge Email Handling

Status: partial.

Design Spec reference: section 4.1 says huge emails should store a preview and first N chunks, then lazy-fetch more on explicit view.

Current state:

- Preview is stored.
- Full body text is stored for fetched messages.
- Search indexing creates one chunk per document.
- No lazy body continuation or body chunk paging exists.

Remaining work:

- Split mail bodies into multiple chunks.
- Cap default indexed body size.
- Add continuation metadata for `mail.view`.
- Add explicit lazy-fetch behavior or document that the MVP only indexes locally stored bodies.

### Encrypted Or Unavailable Message Bodies

Status: not implemented.

Design Spec reference: section 4.1 says S/MIME or encrypted bodies should index metadata only and return `body_unavailable_reason`.

Current state:

- No encrypted body detection exists.
- `mail.view` does not return `body_unavailable_reason`.

Remaining work:

- Detect encrypted/signed-only MIME structures.
- Store body availability status.
- Return `body_unavailable_reason` in `mail.view`.

### Quoted Reply Chain Handling

Status: not implemented.

Design Spec reference: section 4.1 says full text should be stored, while cleaned reply chunks should down-weight quoted text.

Current state:

- Body text is normalized line-by-line.
- No reply quote stripping or down-weighting exists.
- Search ranking treats quoted text like original content.

Remaining work:

- Create cleaned body chunks that suppress common quoted reply markers.
- Keep full raw body text available for view.
- Add tests for matching current reply text over quoted history.

### Spam And Newsletter Ranking

Status: partial.

Design Spec reference: section 4.1 says spam/newsletters should rank lower unless exact match or user filters include those folders.

Current state:

- IMAP folder selection skips trash, deleted, junk, and spam folders by default.
- No ranking penalty exists for noisy folders already present in the cache.
- Newsletter classification is not implemented.

Remaining work:

- Store mailbox/folder quality metadata.
- Add ranking penalties for spam/junk/newsletter folders.
- Allow explicit folder filters to override the penalty.

### Older Mail Backfill

Status: not implemented.

Design Spec reference: sections 8.1 and 8.2 require recent mail first and gradual older mail body backfill.

Current state:

- Mail sync fetches a configurable recent date window.
- No separate background backfill worker exists.
- No backfill status is exposed.

Remaining work:

- Add a backfill worker with checkpointed progress per mailbox.
- Expose `mail_backfill_status` in search freshness metadata.
- Ensure normal interactive search remains fast while backfill runs.

## 3. Calendar Adapter And Calendar Writes

### WebDAV Sync Token Support

Status: not implemented.

Design Spec reference: section 4.2 says CalDAV sync should prefer WebDAV sync-token and `sync-collection` when supported.

Current state:

- Calendar sync runs time-window searches.
- `calendar_collections.sync_token` exists but is not populated from sync-token based sync.
- No `sync-collection` logic exists.

Remaining work:

- Fetch and store collection sync tokens.
- Use `sync-collection` for changed/deleted resources.
- Fall back to windowed `calendar-query` when sync-token is unsupported.

### Calendar CTag Sync

Status: partial.

Design Spec reference: section 4.2 says calendar URL, display name, color, ETag, ctag, and sync token should be stored.

Current state:

- Calendar URL, display name, color, read-only state, and event ETags are stored.
- `ctag` column exists.
- CalDAV adapter does not populate calendar ctag.

Remaining work:

- Extract calendar ctag where available.
- Use ctag to skip unchanged calendar collections when sync-token is unavailable.

### Deleted Calendar Object Handling

Status: not implemented.

Design Spec reference: section 8.3 requires changed objects to update indexes, and sync-token support implies deletion handling.

Current state:

- Upserts clear `deleted_at`.
- No sync path marks removed remote events as deleted.
- Search documents and occurrences for deleted events are not pruned from remote deletion signals.

Remaining work:

- Detect remote 404/deleted resources.
- Mark `calendar_objects.deleted_at`.
- Remove or tombstone related occurrences and search documents.

### Recurrence Exceptions

Status: partial.

Design Spec reference: section 4.2 says recurring events must be expanded into occurrence rows and re-expanded on changes, and update scope includes `single`, `future`, and `series`.

Current state:

- Basic RRULE expansion exists for simple recurrence rules.
- Cancelled event status is stored.
- `RECURRENCE-ID` is stored.
- EXDATE, RDATE, detached overridden occurrences, and cancelled individual instances are not handled.
- No recurrence exception merge logic exists.

Remaining work:

- Parse and apply `EXDATE` and `RDATE`.
- Merge master events with detached `RECURRENCE-ID` exceptions.
- Track cancelled individual occurrences.
- Add tests for recurring event exceptions.

### Calendar Update Scopes `single` And `future`

Status: not implemented.

Design Spec reference: section 4.2 defines update scopes `single`, `future`, and `series`; MVP may support series first.

Current state:

- `update_calendar_event` only supports `scope="series"`.
- `single` and `future` return `unsupported_scope`.

Remaining work:

- Implement single-occurrence updates through detached recurrence exceptions.
- Implement future-occurrence updates by splitting the series.
- Add conflict tests for each scope.

### Unknown ICS Property Preservation

Status: partial.

Design Spec reference: section 4.2 says calendar update must preserve unknown ICS properties.

Current state:

- Raw ICS is stored.
- Local update rebuilds ICS with `build_ics`.
- Rebuilding can drop unknown VEVENT properties, VTIMEZONE details, custom X-properties, organizer details, and alarms not included in the patch.

Remaining work:

- Parse the existing `raw_ics`.
- Apply only requested patch fields.
- Preserve unmodified components and properties.
- Add tests with custom `X-` properties and VTIMEZONE data.

### Remote `If-Match` Write Semantics

Status: partial.

Design Spec reference: section 4.2 says update should PUT with `If-Match` ETag and return conflict on mismatch.

Current state:

- The adapter fetches current ETag before save and returns conflict if it differs.
- The code does not visibly issue a CalDAV PUT with an `If-Match` header.
- Actual behavior depends on the `caldav` library `save()` implementation.

Remaining work:

- Confirm or implement explicit `If-Match` behavior.
- Add integration test against a fake CalDAV server that enforces ETags.

### Immediate Post-Write Sync Semantics

Status: partial.

Design Spec reference: section 4.2 says create and update should trigger local sync/index update immediately.

Current state:

- Local cache and search index are updated immediately from the write result.
- No follow-up remote sync is scheduled to verify server-normalized fields.

Remaining work:

- Re-fetch the written CalDAV object after create/update, or schedule a targeted sync.
- Store any server-normalized ICS and final ETag.

## 4. Contacts Adapter And Contact-Aware Search

### CardDAV Incremental Sync

Status: not implemented.

Design Spec reference: sections 4.3 and 8.3 require efficient changed-object sync and checkpointing.

Current state:

- Addressbook sync tokens and ctags are stored.
- Contacts sync fetches all vCards each run with `addressbook-query`.
- No sync-token or ctag-based incremental logic is used.
- No remote deletion handling exists.

Remaining work:

- Use CardDAV sync-token where supported.
- Fall back to ctag comparison plus targeted refetch.
- Mark deleted contacts and remove aliases/search docs.

### Contact Alias Quality

Status: partial.

Design Spec reference: section 4.3 says aliases should include full name, given name, family name, email local parts, nicknames, phonetic names if available, organization, and relations.

Current state:

- Full name, email address, given name, family name, and organization are indexed.
- Email local parts are not separately generated.
- Nicknames, phonetic names, and relations are not parsed.
- All alias rows use `alias_type="contact"` instead of more precise alias types.

Remaining work:

- Extract email local parts.
- Parse vCard nickname, phonetic name, and related/contact relation fields.
- Store meaningful alias types and confidence values.

### Contact-Aware Calendar Search

Status: partial.

Design Spec reference: Phase 3 requires contact-aware calendar search.

Current state:

- `icloud.search` can expand explicit `person` filters through contact aliases.
- Calendar search does not automatically extract people from the free-form query.
- Query `"meeting with Liesa"` relies on direct FTS/semantic matching, not a full alias expansion pipeline.

Remaining work:

- Extract person entities from natural-language query text.
- Expand aliases automatically even when the caller does not pass `person`.
- Use contact emails/names to boost calendar attendee matches.

## 5. Unified Search And RAG

### Query Planner Pipeline

Status: partial.

Design Spec reference: section 7.2 requires normalizing queries, resolving relative dates, detecting domain intent, extracting entities, expanding aliases, running lexical/vector/structured search, fusing, reranking, generating snippets and answer hints, and caching.

Current state:

- Query normalization exists.
- A simple query planner module exists but is not integrated into `icloud.search`.
- Explicit `start`, `end`, and `person` filters work.
- Relative date resolution is not implemented.
- Domain intent detection is not used for search routing or ranking.
- Automatic entity extraction is not implemented.
- Alias expansion works only for explicit `person`.
- Lexical and deterministic hashed semantic scoring exist.
- Reranking is minimal and does not implement the specified formula.
- Query cache exists and is used.

Remaining work:

- Wire a real `QueryPlan` into `icloud.search`.
- Resolve relative dates such as "tomorrow", "last week", "next meeting".
- Detect intents such as calendar time lookup, mail search, person lookup, and event listing.
- Extract people/entities from raw query text.
- Apply RRF-style fusion and domain-specific boosts.

### Durable Vector Backend

Status: partial.

Design Spec reference: sections 6.1, 7.1, and Phase 5 call for SQLite-backed local embeddings such as `sqlite-vec`, vector backend abstraction, and an embedding worker.

Current state:

- A deterministic hashed bag-of-words semantic score exists in `src/icloud_mcp/indexing/vector.py`.
- `EmbeddingWorker` marks chunks as ready.
- No vector table or persisted embedding values exist.
- `sqlite-vec` or equivalent is not used.

Remaining work:

- Add a vector storage backend or explicitly revise the design to accept hashed in-memory scoring.
- Persist embeddings per chunk.
- Batch embedding jobs asynchronously.
- Search lexical results and vector results separately before fusion.

### Reranking Formula

Status: partial.

Design Spec reference: section 7.4 specifies a weighted ranking formula with lexical, vector, entity, time, domain intent, freshness, and source quality scores.

Current state:

- FTS BM25 order is used for lexical matches.
- Hashed semantic matches can be appended.
- Some rows include `why`.
- No weighted scoring formula exists.
- No freshness, source quality, or domain-intent score is applied.

Remaining work:

- Implement separate scoring signals.
- Fuse scores with documented weights or a tested equivalent.
- Add tests for exact alias match, email match, upcoming event boost, "what time" boost, and noisy-folder penalty.

### Per-Occurrence Search Documents

Status: partial.

Design Spec reference: section 7.3 says calendar indexing should create one document for the event master and one lightweight document per occurrence in the search window.

Current state:

- Occurrence rows are expanded.
- Search indexing creates one calendar search document per event master.
- No separate per-occurrence search documents are created.

Remaining work:

- Create occurrence-level search documents with `occurrence_id`.
- Include occurrence start/end in metadata.
- Return occurrence source IDs in answer hints when appropriate.

### Multi-Chunk Search Documents

Status: partial.

Design Spec reference: sections 6.2 and 7.3 define `search_chunks` and describe header/body/invite chunks.

Current state:

- `upsert_search_document` writes exactly one chunk at `chunk_index=0`.
- There is no mail header chunk vs body chunks vs invite chunk separation.
- Token budget is not enforced per chunk beyond storing token counts.

Remaining work:

- Split documents into chunks by domain-specific policy.
- Index each chunk separately.
- Return snippets from the best matching chunk.

### Search Structured Output Pattern

Status: partial.

Design Spec reference: section 9.2 recommends FastMCP structured tool output with `content`, `structured_content`, and `meta`.

Current state:

- Tools return plain dictionaries.
- `icloud.search` includes a `meta` field.
- It does not return FastMCP `ToolResult` or a `structured_content` wrapper.

Remaining work:

- Decide whether to adopt FastMCP `ToolResult`.
- If adopted, keep machine-readable data under `structured_content`.
- Preserve compact text content for clients that display content.

### Freshness Policy Behavior

Status: partial.

Design Spec reference: section 8.4 requires freshness in every search result and says stale indexes must not be silently treated as complete.

Current state:

- Search returns `mail_last_sync`, `calendar_last_sync`, and `contacts_last_sync`.
- `mail_backfill_status` is missing.
- `freshness_policy="refresh_if_stale"` bypasses cache but does not trigger refresh.
- Staleness thresholds are not evaluated.

Remaining work:

- Add per-domain freshness thresholds.
- Add `mail_backfill_status`.
- Implement explicit refresh behavior or return a clear "refresh unavailable/offline" status.

### Answer Hints

Status: partial.

Design Spec reference: sections 5.2 and 7.2 require compact deterministic `answer_hints`.

Current state:

- One `calendar_time` hint is generated when the top result is calendar and query tokens include time or meeting.
- No hints exist for people, mail date ranges, event lists, contact identity, or ambiguous candidates.
- Hints use event IDs, not occurrence IDs.

Remaining work:

- Add hints for person lookup, upcoming meeting, mail sender/date queries, and ambiguous candidate sets.
- Use occurrence source IDs when the relevant evidence is an occurrence.

## 6. MCP Tool Surface

### Calendar Write Input Schemas

Status: partial.

Design Spec reference: section 5.5 shows `CreateEventInput`, `UpdateEventInput`, and `CalendarWriteResult`.

Current state:

- Calendar write tools accept raw `dict` input.
- Validation is manual.
- Schema dataclasses exist but are not wired into FastMCP tool signatures.

Remaining work:

- Replace raw dicts with typed Pydantic models or dataclasses supported by FastMCP.
- Keep manual validation where server-side checks need richer rules.

### Tool Wrappers With Full Filter Support

Status: partial.

Design Spec reference: section 5 says domain-specific search tools should wrap unified search.

Current state:

- `icloud.mail.search` and `icloud.calendar.search_events` wrap unified search.
- They expose only `query`, `limit`, and `cursor`.
- They do not expose `start`, `end`, `person`, `include_body_snippets`, or freshness policy.

Remaining work:

- Decide which unified filters should be exposed by wrappers.
- Add date and person filters where useful.

### Delete Tool Exclusion

Status: implemented.

No remaining work. There is no calendar delete tool.

## 7. Database And Indexing Schema

### External-Content FTS Table

Status: partial.

Design Spec reference: section 6.2 says `search_fts` should be an external-content FTS5 table using `content='search_chunks'` and `content_rowid='rowid'`.

Current state:

- `search_fts` is an FTS5 table.
- It is not configured as an external-content table.
- FTS rows are manually deleted and inserted.

Remaining work:

- Either convert `search_fts` to external-content mode or document why manual FTS rows are preferred.
- If converted, add migrations and rebuild logic.

### Schema Migrations

Status: not implemented.

Design Spec reference: section 14 says the server should run migrations.

Current state:

- Schema is embedded in `src/icloud_mcp/db/connection.py`.
- `src/icloud_mcp/db/migrations/` exists but has no migration framework.
- Existing databases are not versioned.

Remaining work:

- Add schema version table.
- Add migration runner.
- Move schema changes into migrations.

### Foreign Keys And Referential Cleanup

Status: partial.

Design Spec reference: implied by normalized local database design.

Current state:

- SQLite foreign keys are enabled.
- Tables do not define foreign key constraints.
- Deleting or tombstoning source objects does not automatically clean related chunks, FTS rows, aliases, or occurrences.

Remaining work:

- Add foreign key constraints where practical.
- Add repository cleanup paths for tombstones.
- Test cleanup behavior.

## 8. Reliability And Performance

### Latency Targets

Status: not verified.

Design Spec reference: section 11.1 defines local cache read and search latency targets.

Current state:

- No benchmark tests exist.
- No timing metadata is emitted for tools.
- No performance regression guard exists.

Remaining work:

- Add benchmark or smoke timing tests for list/view/search.
- Add timing metadata or internal metrics.

### Backoff, Retries, And Circuit Breakers

Status: not implemented.

Design Spec reference: Phase 6 requires backoff and circuit breakers.

Current state:

- `tenacity` is listed as a dependency.
- Sync workers call adapters directly.
- No retry policy, exponential backoff, circuit breaker, or temporary disable state exists.

Remaining work:

- Wrap protocol calls with bounded retry/backoff.
- Track repeated failures per domain.
- Expose circuit state in `sync.status`.

### Worker Progress And Checkpoints

Status: partial.

Design Spec reference: section 11.2 says each worker must have last success time, last error, progress cursor, retry/backoff state, and cancellation signal.

Current state:

- Checkpoints record status, last sync time, and detail JSON.
- No progress cursor is stored for mail backfill or DAV sync-token progress.
- No retry/backoff state exists.
- Scheduler has a process-level stop event, but individual workers do not expose cancellation.

Remaining work:

- Store progress cursors per worker.
- Store last error separately from status detail.
- Add retry/backoff fields.
- Allow long workers to observe cancellation.

### Offline Write Behavior

Status: partial.

Design Spec reference: section 11.3 says search/list/view should work from cache and calendar writes should return clear connectivity errors without queueing by default.

Current state:

- Search/list/view work from local cache.
- Missing credentials return clear errors.
- Remote connectivity failures are caught in scheduler but not consistently normalized for calendar write tools.
- There is no explicit "offline" classification for write failures.

Remaining work:

- Normalize network/auth failures in calendar write tools.
- Return clear `connectivity_error`, `credential_revoked_or_expired`, or similar statuses.
- Ensure writes are not queued unless a future explicit queue setting exists.

### Stale Index Reporting

Status: partial.

Design Spec reference: sections 8.4 and 11.3 require stale freshness metadata.

Current state:

- Last sync timestamps are returned.
- No stale/healthy classification is computed.
- No stale threshold exists.

Remaining work:

- Add freshness age and status per domain.
- Include stale reasons in search and sync status.

## 9. Security And Local Hardening

### OS Keychain Credential Storage

Status: not implemented.

Design Spec reference: section 10.1 says credentials should be configured out-of-band and OS keychain is preferred.

Current state:

- Credentials load from environment variables.
- `keyring` is a dependency but is not used.
- Setup scripts can write secrets to env files with `chmod 600`.

Remaining work:

- Add keychain read/write support.
- Keep environment variable support as a fallback.
- Update setup scripts to prefer keychain storage.

### Credential Rotation And Revocation Detection

Status: partial.

Design Spec reference: section 10.1 says password rotation should be supported and auth failures should surface a clear credential revoked or expired status.

Current state:

- Changing env vars rotates credentials.
- Missing credentials are reported.
- Auth failures from protocol libraries are not normalized to a credential-specific status.

Remaining work:

- Catch protocol auth exceptions.
- Return and store `credential_revoked_or_expired` or equivalent.
- Add tests for auth failure paths.

### Redaction In Logs And Tool Metadata

Status: partial.

Design Spec reference: section 10.1 says never log app-specific passwords and redact emails/message bodies in debug logs unless explicitly enabled.

Current state:

- Redaction helpers exist.
- Audit logging avoids body/secret content.
- Redaction helpers are not wired into all logging paths.
- There is no config flag for allowing unredacted debug content.

Remaining work:

- Apply redaction to all exception/detail output that can include protocol URLs, email addresses, or message text.
- Add tests for secret and email redaction.

### Prompt Injection Handling

Status: partial.

Design Spec reference: section 10.4 requires treating mail bodies, calendar descriptions, contact notes, and invite text as untrusted data.

Current state:

- Tool descriptions do not include hidden instructions.
- Outputs are compact by default.
- Search snippets and view bodies are returned as plain data without explicit source labeling or untrusted-content markers.

Remaining work:

- Label retrieved content as user data / untrusted source content.
- Avoid embedding retrieved text into tool descriptions or metadata fields interpreted as instructions.
- Add security tests for hostile mail/calendar/contact content.

### Dependency Pinning And SBOM

Status: partial.

Design Spec reference: section 10.4 and Phase 7 require pinned dependencies, lockfile, SBOM, and CI security checks.

Current state:

- `uv.lock` exists.
- No SBOM generation exists.
- No CI security check configuration is present in the visible file list.

Remaining work:

- Add SBOM generation command or workflow.
- Add dependency/security scanning in CI.
- Document the security check process.

### Public Network Exposure Guard

Status: partial.

Design Spec reference: section 10.2 says the server must run locally over STDIO and not be exposed as a public/shared network service.

Current state:

- `mcp.run()` uses FastMCP defaults.
- README says STDIO only.
- There is no explicit runtime assertion preventing future HTTP transport use.

Remaining work:

- Keep STDIO as the only configured transport.
- Add documentation or tests that reject network transport configuration if introduced later.

## 10. Observability

### Metrics Endpoint

Status: not implemented.

Design Spec reference: Phase 7 requires a metrics endpoint.

Current state:

- `TimingMetric` dataclass exists.
- No endpoint or MCP tool exposes metrics.
- No counters/timers are recorded.

Remaining work:

- Decide whether metrics should be an MCP tool, local file, or local-only HTTP endpoint.
- Record sync duration, tool latency, failure counts, indexed object counts, and cache hit rate.

### Structured Timing Metadata

Status: partial.

Design Spec reference: section 9.2 recommends execution time, cache status, and index generation metadata.

Current state:

- `icloud.search` returns cache status and index generation.
- Other tools do not return timing/cache metadata.
- Execution time is not measured.

Remaining work:

- Add middleware or wrapper timing.
- Include compact metadata consistently where useful.

## 11. Testing

### Unit Test Coverage From Spec

Status: partial.

Design Spec reference: section 15.1 lists unit tests for query planner, aliases, mail parser, ICS parser, vCard parser, ranking, token limits, and cursors.

Current state:

- Parser tests exist for IMAP HTML, vCard, and ICS.
- Local tests cover calendar create/update conflict, recurrence expansion, cursors, contact search, query cache, and local search indexing.
- Query planner tests are minimal or absent.
- Ranking formula tests are absent.
- Token-limit edge case tests are partial.
- Alias variant tests for nicknames, local parts, and relations are absent.

Remaining work:

- Add query planner tests for names, dates, "what time", "next", and "last".
- Add ranking tests for exact vs semantic and upcoming vs old.
- Add token truncation tests for snippets, titles, body views, and raw ICS/vCard inclusion.

### Protocol Integration Tests

Status: not implemented.

Design Spec reference: section 15.2 requires fake protocol servers for IMAP, CalDAV, and CardDAV.

Current state:

- Sync workers use fake in-memory adapters in unit tests.
- No GreenMail, fake IMAP server, Radicale, or fake DAV server integration tests exist.

Remaining work:

- Add fake IMAP integration tests for initial sync, incremental sync, message body search, credential failure, and deletes.
- Add fake CalDAV tests for recurrence search, create, update, ETag conflict, and deleted events.
- Add fake CardDAV tests for alias search, incremental sync, and deleted contacts.

### Golden Prompt Tests

Status: not implemented.

Design Spec reference: section 15.3 lists fixed prompts and expected behavior.

Current state:

- No golden prompt test harness exists.

Remaining work:

- Add deterministic tests for:
  - "What time is my meeting with Liesa?"
  - "Show emails from Liesa last week."
  - "Find the email where someone mentioned the contract deadline."
  - "Who is Liesa?"
  - "What meetings do I have tomorrow?"
  - "Create a calendar event tomorrow at 10 called Focus Time."
  - "Move my Project Sync with Liesa to 15:00."

### Security Test Suite

Status: not implemented.

Design Spec reference: Phase 7 requires a security test suite.

Current state:

- No dedicated security tests are present.

Remaining work:

- Test credential redaction.
- Test prompt-injection content handling.
- Test cursor tampering.
- Test calendar write validation boundaries.
- Test no credential fields are accepted as tool arguments.

## 12. Documentation

### Current Capability Documentation

Status: partial.

Design Spec reference: overall design describes several future phases.

Current state:

- README says the repo implements design scaffold and local MVP.
- README also says protocol adapters and sync workers are ready, but does not precisely list the gaps above.

Remaining work:

- Link this file from README.
- Add "Implemented", "Partial", and "Not implemented" capability tables.
- Document operational limits, especially recurrence exceptions, incremental sync gaps, and attachment indexing.

