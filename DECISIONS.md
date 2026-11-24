# Decisions log

Design questions settled one at a time (grill-me style), newest last.

## 2026-11-13 — v1
See `SPEC.md`: per-company self-hosted; Postgres as the only system of record; append-only versions with
provenance; six maintainers with policy-driven risk tiers that fail closed; AIDBHarness via MCP + web page;
push-based ingestion; Python/FastAPI/psycopg stack; team-scoped access control.

## 2026-11-21 — direction for v1.x
- **Q: What does "better" mean next?** Make the core trustworthy at company scale (current-version
  pointer, chunked embeddings + HNSW, one version per enrichment, redaction/erasure, an `ask` tool with
  citations, CI) before connectors. Owner asked to proceed in a loop; recorded in `ROADMAP.md`.

## 2026-11-21 — current-version pointer (`item_heads`)
- **Q: App code or trigger maintains the head?** Trigger (`AFTER INSERT` on `item_versions`). Every
  write path, including future ones and raw SQL, keeps it right in the same transaction. Rollback drops it too.
- **Q: What does a head row hold?** `item_id` (pk), `version_id` (fk to the version row), `version`.
  The view joins on `version_id`, which is the primary key, so lookups are index-only joins.
- **Q: Can heads be edited?** Only forward: a guard trigger rejects deletes and any update that doesn't
  move to an existing later version of the same item. That keeps it derived data, not a second history.
- **Q: Migration of existing data?** New migration `002_item_heads.sql` backfills from `item_versions`
  and replaces the `items_current` view with the same columns, so callers are unchanged. Not destructive.
- **Q: Use heads for the next version number?** Yes, in `store._insert_version` (already serialised by the
  `items` row lock). Other `max(version)` lookups use the `(item_id, version)` unique index and stay as is.
- **Q: Full-text search?** Out of scope. The GIN index is still on `item_versions`. Revisit with the
  chunking item if search over current versions gets slow.

## 2026-11-21 — chunked embeddings (part 1 of "Chunked embeddings + vector index")
The roadmap item is split. Part 1 (this run): chunking and chunk-level search. Part 2: fixed dims per model
and an HNSW index. Part 2 needs pgvector, which the local test Postgres doesn't have, so it is done together
with (or after) CI against `pgvector/pgvector:pg16`.
- **Q: New table or reuse `item_embeddings`?** New `item_chunks` table (`003_item_chunks.sql`), one row per
  (item, model, chunk_no). `item_embeddings` stays as the whole-item vector, because dedup and linking compare
  whole items. Both are derived and rebuildable, so replacing an item's chunks (delete + insert) is allowed.
- **Q: Store the chunk text?** No. Store `start_char`/`end_char` offsets into the body and the `content_hash`
  they were cut from; the snippet is cut from the current body at read time. That avoids a second copy of the
  content, which the upcoming redaction/erasure item would also have to scrub.
- **Q: What if the item changed since it was chunked?** Search only uses chunks whose `content_hash` matches
  the current version. Enrichment versions are metadata-only (same hash), so they don't invalidate chunks.
  Changed content queues `enrich` (as today), which re-chunks; hygiene re-queues items with no valid chunks,
  which also backfills existing data after the migration.
- **Q: How to split?** Paragraphs (blank-line separated) packed greedily up to `DB4AI_CHUNK_CHARS`
  (default 1500); a paragraph longer than that is cut at the last whitespace before the limit. No overlap
  for now. Each chunk is embedded as `title + "\n" + chunk text` so short chunks keep their context.
  An item with an empty body has one empty chunk, embedded on its title.
- **Q: What does search return?** Unchanged fields plus `chunk` (`no`, `start`, `end`, `text`) for the best
  matching chunk of each item by vector score; the item's vector score is its best chunk's score. Items found
  only by full-text search have `chunk: null`.
- **Q: Batch limits?** `VoyageEmbedder.embed` now sends at most 128 inputs per request (Voyage's limit).
- **Q: Roadmap order?** CI moves up to right after this item, because the vector index (part 2) needs
  pgvector in tests and CI is where it gets that.

## 2026-11-22 — CI
- **Q: Which databases?** Two jobs from one matrix: `pgvector/pgvector:pg16` (the image `docker-compose.yml`
  ships) and plain `postgres:16`, so both embedding backends (`vector` and the `float8[]` fallback) stay tested.
  The local test cluster usually has no pgvector, so CI is the only place the `vector` path runs.
- **Q: How do tests reach the service Postgres?** The existing `DB4AI_TEST_DATABASE_URL` hook in
  `tests/conftest.py` (template database + one clone per test). No conftest changes needed.
- **Q: How do we know pgvector was really used?** New `DB4AI_EXPECT_PGVECTOR=1|0`; a test asserts the
  `item_embeddings`/`item_chunks` column types match. Unset (local runs) it skips. Without it a broken
  pgvector install would silently fall back and CI would still be green.
- **Q: Python versions?** 3.12 (the Dockerfile's) on both databases, plus 3.11 (the `requires-python` floor)
  on pgvector. Newer versions can be added when the Dockerfile moves.
- **Q: What does the smoke test cover?** `scripts/smoke.sh`: `docker compose up --build` of api + worker
  (migrate runs first), wait for `/healthz`, check the chunk column is `vector`, create an admin user via the
  CLI, `POST /items`, `GET /items/{id}`, `db4ai maintain --once`, then `/search` must return the item with a
  chunk (retried up to 30s because the worker races the one-shot run). Uses a separate compose project name
  and `down -v` on exit, so it never touches a developer's real `db4ai` volumes. Prints logs on failure.
- **Q: Triggers?** Push to `main` and `loop/**` (so loop failure branches get a CI result too) and pull
  requests. `contents: read` only; no secrets, so the offline heuristic LLM and hash embedder are used.
- **Not verified locally:** this machine has no Docker or pgvector, so the first real run of the `vector`
  path and the smoke test is the first CI run after this push.

## 2026-11-22 — vector index (part 2 of chunked embeddings)
- **Q: How do we get fixed dims when one `vector` column holds several models?** A registry table
  `embedding_models (model, dims)` (`004_vector_index.sql`), filled on a model's first write and checked on every
  write and query (`ValueError` on mismatch). The column stays untyped `vector`; each model gets a **partial
  expression index** `hnsw ((embedding::vector(dims)) vector_cosine_ops) where model = '<model>'` on both
  `item_embeddings` and `item_chunks`. That avoids one table per model and keeps switching embedders possible.
- **Q: Who creates the indexes?** SQL function `db4ai_ensure_vector_index(model, dims)`, called by the migration
  for existing models and by `store.register_model` when a new model is first seen (same transaction as the first
  embedding). No-op without pgvector and above pgvector's 2000-dim HNSW limit (exact scan for those).
- **Q: Existing rows with mixed dims for one model?** The migration registers the most common size and deletes
  embedding/chunk rows of other sizes (they would break the index build). They are derived data; hygiene re-queues
  `enrich` for items with no valid embedding/chunks, so they are rebuilt. Not owner-level: nothing primary is lost.
- **Q: HNSW plus team/type filters can miss results. How do we keep recall?** Two passes in `store._nearest`:
  take the `DB4AI_VECTOR_CANDIDATES` (default 200, max 1000) nearest rows by `order by distance limit`
  (an index scan, with `hnsw.ef_search` raised to match), then filter. If fewer than `limit` results survive and
  the pool was full, rerun without the limit (exact). Results are identical to an exact scan; only speed differs.
  pgvector 0.8 iterative scans were not used, so older pgvector versions work too.
- **Q: Why inline the model name in the SQL?** A partial index is only usable if the planner can prove
  `model = '<model>'`; with a bound parameter a generic (prepared) plan can't, and psycopg prepares statements
  after 5 runs. The model is quoted with `psycopg.sql.Literal`.
- **Q: Same queries on the `float8[]` fallback?** Yes: the distance expression is `1 - db4ai_cosine(...)`
  instead of `<=>`, the rest of the query (and the fallback logic) is shared, so local tests exercise it.
- **Not verified locally:** no pgvector here, so the HNSW index creation and the `EXPLAIN` test that checks the
  index is used run for the first time in CI (`pgvector/pgvector:pg16`).

## 2026-11-22 — one version per enrichment
- **Q: New action or batch the three existing ones into one version?** A new registered action `enrich` with
  payload `{item_id, summary?, tags?, entities?, for_version}`. One harness decision, one `new_version`, one diff
  showing before/after per field, and one revert. Batching three decisions into one version would leave three
  decisions pointing at one version and make per-decision revert ambiguous.
- **Q: Keep `add_tags` / `set_summary` / `set_entities`?** Yes. Existing harness decisions reference them (revert
  must still work) and they are useful for single-field edits. The maintainer just stops proposing them.
- **Q: What tier does `enrich` get?** Its own policy row, global `auto` (migration `005_enrich_action.sql`), same as
  the three it replaces. Any team that had overridden one of the three gets a team `enrich` row with the
  **strictest** of its overrides (fail closed), so a team that made `set_summary` ask_first doesn't silently get
  auto enrichment. Later changes to the per-field policies do not affect `enrich`; set the `enrich` policy instead.
- **Q: Empty fields?** Omitted from the payload; if all three are empty, nothing is proposed (no empty version).
- **Q: Tags merge or replace?** Merge with existing tags, as `add_tags` did (shared `_merge_ai` helper).
- **Q: Rejection fingerprint?** Unchanged mechanism: the fingerprint covers the whole payload minus
  `for_version`, so a rejected enrichment isn't re-proposed with identical values.

## 2026-11-22 — redaction and erasure via the AIDBHarness
- **Q: One action or two?** Two registered actions: `erase_item {item_id, reason}` (wipe all content) and
  `redact_item {item_id, reason, terms}` (replace terms with `[redacted]`, keep the rest). Different diffs,
  different effects on blobs and enrichment, one shared module (`db4ai/erasure.py`).
- **Q: Who can ask, who can approve?** Anyone who can read the item can ask (HTTP, MCP, or `erasure.request`), and a
  reason is required. Only admins can approve (`harness.ADMIN_ONLY`), not team owners. The tier is forced to
  `ask_first` in code, so a policy row set to `auto` can't loosen it (fail closed). Neither action can be reverted.
- **Q: How do we bypass the append-only triggers safely?** `006_erasure.sql` replaces the `item_versions`, `items` and
  `change_log` triggers. An `UPDATE` passes only while `db4ai.erasure` (transaction-local) names a *pending*
  `erase_item`/`redact_item` decision, only for that decision's item, and only if nothing changes except the content
  columns (title, body, metadata, blob, content hash, reason; `external_key` on `items`; before/after/reason on
  `change_log`). Deletes are always blocked. The setting is set and cleared inside the apply, which runs in the
  approve transaction. Anyone with raw SQL could set it too, but they could also drop the triggers; this guards the
  app against its own bugs, not a DBA.
- **Q: What exactly is scrubbed?** Every version of the item; its `change_log` rows and those written under decisions
  that mention it; those decisions' payload (erase keeps only id keys), diff, evidence, reason and note; link reasons;
  embeddings and chunks (deleted: they're derived). Erase also nulls blobs and hashes the external key. Ids, version
  numbers and provenance stay, so history still shows who changed what and when.
- **Q: Tombstone?** An append-only `erasures` row (kind, versions, term count, matches, blobs deleted/kept, requester,
  approver, reason) plus a final version: `erased` status for erase (no further writes, `store` refuses them), or a
  `redactions` metadata entry for redact. The tombstone never stores the terms or content hashes of the old content.
- **Q: Blobs?** Content-addressed, so another item may share the bytes: those are kept (counted as `blobs_kept`).
  Otherwise the `blobs` row is deleted in the transaction and the file by a `blob_delete` outbox job after commit, so a
  row never points at a missing blob. If the job is lost, hygiene's orphan GC removes the file anyway.
- **Q: Re-import after erase?** The external key becomes `erased:<sha256(key)>`; `save_item` sees it and returns
  `{"erased": true, "changed": false}` instead of creating a new copy. A redacted item's key is hashed the same way
  only if it contains a term. Re-importing an unredacted source over a redacted item will bring the text back: redact
  the source too.
- **Q: Redaction terms are themselves sensitive. Where do they end up?** Scrubbed from the request decision itself
  after applying (payload, diff, reason), and from the reason on the approval's change_log row (`decide` now re-reads
  the stored reason after apply). An edited payload is stored *before* apply so its terms get scrubbed too. Matching is
  case-insensitive, longest term first.
- **Q: Data outside the item (merge aliases, `promote_schema` rows in `data.*`, quotes in other items)?** Out of scope
  for this run; added to `ROADMAP.md` as "Erasure of copied content". Not an owner question: it's more of the same
  mechanism.
- **Q: Is building this an owner-level decision?** No: the owner put it on the roadmap. It only deletes data when an
  admin approves a specific request, and nothing existing is migrated or removed by the migration.

## 2026-11-22 — erasure of copied content
- **Q: Which copies are in scope?** Three: (1) rows in `data.*` tables that carry the item's id in an `item_id` column
  (the `promote_schema` backfill, or any dataset with that column) and their `change_log` rows; (2) merge aliases: the
  erased item's title in the `aliases` of the item it was merged into; (3) other items whose title, body or metadata, in
  any version, contain the erased text (quotes, enrichment summaries that repeat it).
- **Q: Scrub them in the same transaction or offer them?** Depends on how sure we are that it's a copy. `data.*` rows
  keyed by the item id are unambiguously derived from it, so they are scrubbed in the same approved transaction (erase
  nulls every column except `_row_id`, `item_id`, `_created_at`; redact replaces terms in text columns). Text in other
  items may be a legitimate quote, so each such item gets its own `redact_item` proposal (ask_first, admin-only, as
  always) for an admin to approve or reject one by one. That's what the roadmap asked for ("find and offer").
- **Q: What counts as "the erased text" when looking in other items?** For redact: the terms themselves. For erase:
  sentences of 40+ characters from any version's body (verbatim, case-insensitive), at most 200 of them, because short
  phrases and titles ("Weekly sync") match far too much. The item's titles are only searched in items it was merged
  into (that's where the alias copy lives), whatever their length. Each proposal only names the passages found in that
  item.
- **Q: How many proposals?** At most 100 per erasure (`erasure.MAX_COPY_PROPOSALS`); the result and tombstone say
  how many were found and whether the list was cut. Already-erased items are skipped. Rejected proposals are not made
  again (fingerprint), as with any decision.
- **Q: Doesn't a pending proposal hold erased text?** Yes, it has to name what to redact. That text is still in the
  copy item anyway, so it adds no new exposure; approving it scrubs both (redact scrubs its own request). The proposals
  are made after the erased item's own scrub, so they aren't scrubbed by it, and their payload names the source only in
  `copy_of` (not an item ref, so it doesn't widen what the triggers allow).
- **Q: Who proposes them?** The approving admin (`proposed_by`), source `erasure`, reason
  `copy of content from item <id> (erasure <id>)`, which holds no content.
- **Q: How do the triggers allow the `change_log` scrub for `data.*` rows?** Migration `007_erasure_copies.sql`
  extends the `change_log` guard: during an erasure, rows whose entity is `data.*` and whose `before` or `after` has
  `item_id` = the target may change `before`/`after`/`reason`, as long as the new JSON still names the same `item_id`.
  The check reads the row itself, so it works for rows deleted earlier too. Updating the data row fires the capture
  trigger, which logs the old content; that new log row is scrubbed in the same pass.
- **Q: Tombstone?** `erasures` gains `data_rows` and `copies` (proposals made). The admin's diff shows `data_rows` and
  `copies_found` before approving.
- **Not owner-level:** nothing is changed without an admin approving a specific request; the migration only adds
  columns and replaces a trigger function.

## 2026-11-22 — `ask` tool
- **Q: Where does retrieval come from?** `store.search`, unchanged, so team access, active-only, chunk search and its
  exact-scan fallback all apply. Default 8 passages, capped at 20. Each passage is the item's best chunk; items found
  only by full-text search (not chunked yet) use the chunk of their body sharing the most terms with the question.
- **Q: Which version is cited?** The current version (`search` now returns `version`). Chunks are only used when their
  `content_hash` matches the current content, so the cited version really contains the quoted text.
- **Q: Who writes the answer?** A new `LLM.answer(question, passages) -> Answer{answered, answer, citations[passage,
  quote]}`. Claude uses structured output with a system prompt that treats passages as data, not instructions.
  `HeuristicLLM` is extractive: up to two sentences sharing at least two question terms (one for one-term questions).
- **Q: How is grounding enforced?** In code, not by trust (fail closed): citations to passage numbers it wasn't given
  are dropped; a quote that isn't in its passage (case/whitespace-insensitive) is kept with `quote: null`; unless the
  model says `answered` and at least one quote is verbatim, the result is `answered: false` with a fixed "couldn't
  find" answer and no citations. `sources` always lists what was searched.
- **Q: Does `ask` write anything?** No. It's a read, like `search`: no versions, no harness decisions, no log. An audit
  log of questions (and multi-turn context) is a separate roadmap item, since storing questions is a new kind of
  data that erasure would also need to cover.
- **Q: Surfaces?** MCP tool `ask(question, type, limit)` and `POST /ask {question, type, limit}` (POST because questions
  can be long and shouldn't sit in URLs or access logs). Empty or >2000-character questions are a 400.
- **Not owner-level:** read-only, no new services; Claude is used only where it already is (configured credentials).

## 2026-11-22 — notifications for pending harness decisions
- **Q: Which decisions notify?** Only new `pending` ones (ask_first). Auto and auto_notice decisions are already
  applied and visible in `/harness`; notifying them would be noise.
- **Q: How is delivery tied to the decision?** Outbox: `propose` queues a `notify` job in the same transaction, so a
  notification exists iff the decision committed. Only when webhooks are configured (no idle jobs otherwise).
- **Q: One message per decision?** No: an erasure can propose 100 copy redactions at once. Jobs are deduped per
  webhook (`notify:<webhook id>`) and delayed `DB4AI_NOTIFY_DELAY_SECONDS` (default 60), and the job sends one
  message naming every decision still pending, unexpired and not yet sent to that webhook (20 listed, rest counted).
- **Q: How do we know what was sent?** A `notifications` table (decision id, webhook id, sent_at), written in the job's
  commit after the POST succeeds. A crash between POST and commit resends (at-least-once); a failed POST is retried
  by the queue's backoff and records nothing. Decisions decided or expired before the job runs are not named.
- **Q: What goes in the message?** No content: action type, team, confidence, proposer, expiry, decision id and a
  link to `/harness` (`DB4AI_PUBLIC_URL`). Not reason, diff, payload or evidence: those can quote item text (and
  redaction terms), and a Slack/Teams message can't be scrubbed by erasure. The `notifications` table holds ids only,
  so erasure has nothing to scrub there.
- **Q: Where are webhooks configured?** `DB4AI_NOTIFY_WEBHOOKS`, comma-separated, because webhook URLs are secrets and
  shouldn't sit in the database. The DB stores a 16-char sha256 id of each URL; a job whose webhook was removed from
  config is a no-op. Format: `{"text": ...}` (Slack, and legacy Teams connectors) by default; `teams:` prefix sends an
  Adaptive Card for Teams Workflows webhooks.
- **Q: Per-team channels?** Out of scope: every webhook gets every pending decision. Added to ROADMAP as "Per-team
  notification routing".
- **Not owner-level:** no paid services or accounts (the owner supplies a webhook URL if they want it); off by default;
  the migration only adds a table.


## 2026-11-22 — connector framework + meeting transcripts connector
- **Q: Scope for one run?** Split the roadmap item: the framework and a local transcripts connector now; Google Drive
  (needs an OAuth token from the owner, mocked in tests) is its own roadmap item next.
- **Q: Where does a connector run?** Both in-process (`connectors.run`, used by `db4ai sync`) and elsewhere over HTTP
  (`POST /sync/{source}`). Both go through one function, `apply_batch`, which saves records with the same
  `store.save_item` path as `POST /items`.
- **Q: How is the cursor kept consistent with the items?** `sync_cursors` (source, team) row moved in the same
  transaction as the batch's items, so a crash never skips a batch. Compare-and-set on `expected_cursor` (row locked
  `for update`): if another sync moved it, nothing is written and the caller gets a 409. Every move is in `change_log`.
- **Q: Mutable table vs append-only?** The cursor is mutable state like `item_heads`, not company data; history lives
  in `change_log`. It is opaque text that must never hold content, so erasure has nothing to scrub.
- **Q: External keys?** `<source>:<key>`, because `external_key` is unique per (team, type) and two sources (or a file
  import) can share a key like `notes.txt`. Provenance source is `connector:<source>`.
- **Q: Who may sync?** Any actor who can write to the team (same as `POST /items`). Source names are
  `[a-z0-9][a-z0-9_.-]{0,63}` (checked in code and by a table constraint).
- **Q: Batch size?** At most 500 records per batch (bounded transaction); a bad record rolls back the whole batch,
  cursor included.
- **Q: Erased items?** `save_item` already skips withheld keys, so a re-sync can't resurrect them; counted as `erased`.
- **Q: Deletions in the source?** Out of scope: v1 only upserts. Added to ROADMAP ("Source deletions in connectors",
  proposing `archive_item`, ask_first).
- **Q: Transcripts connector shape?** A folder of `.vtt`/`.srt`/`.txt` (where Zoom/Meet/Teams exports land, no
  account needed). Body = "Speaker: text" paragraphs (consecutive cues by the same speaker merged, timing/cue numbers/
  NOTE blocks/markup dropped); metadata `format`, `speakers`, `duration_seconds` (last cue end), `path`; type `meeting`,
  title = file stem; hidden paths skipped. Cursor = newest mtime (ns) synced; files at exactly that mtime are re-sent
  (idempotent) so a file written in the same tick isn't missed.
- **Not owner-level:** no external services or accounts; the migration only adds a table.


## 2026-11-23 — Google Drive/Docs connector
- **Q: Where does it live?** `db4ai/gdrive.py`, a `Connector` (`fetch(cursor) → batches`) used through `connectors.run`,
  so items, cursor moves, idempotency and erased-key withholding all come from the framework. CLI:
  `db4ai sync gdrive --team <t>` (the path argument is now optional and only used by `transcripts`).
- **Q: How does it authenticate?** The owner supplies the OAuth token in the environment, as the roadmap says:
  `DB4AI_GDRIVE_TOKEN` (access token), and/or `DB4AI_GDRIVE_REFRESH_TOKEN` + `DB4AI_GDRIVE_CLIENT_ID` +
  `DB4AI_GDRIVE_CLIENT_SECRET` so a long-running sync survives the 1-hour access-token lifetime (refresh on the first
  401). No OAuth flow and no token in the database: it's a secret, like webhook URLs. Missing token → clear error.
- **Q: What is the cursor?** First sync: take `changes.getStartPageToken` *before* listing, then page through
  `files.list`; mid-listing cursor `list:<start>:<pageToken>` so a crash resumes at the last saved page; afterwards
  `changes:<token>`. Changes made while listing are replayed from `<start>` (re-sending is idempotent). Page tokens are
  opaque and hold no content.
- **Q: Batch size?** One batch per API page (100 files), well under the framework's 500. The last changes page is only
  applied if it has records or the token moved, so an idle sync writes no cursor row or `change_log` entry.
- **Q: Which files?** Google Docs (exported `text/plain`, BOM and CRLF normalised) and `text/plain` / `text/markdown`
  files (downloaded), up to 10 MB (Drive's export limit). Sheets, Slides, PDFs and .docx are skipped in v1; added to
  ROADMAP with folder scoping ("Drive folder scoping and more file types").
- **Q: Which part of Drive?** Everything the token's account can read, shared drives included (`corpora=allDrives`,
  `supportsAllDrives`). Folder scoping needs parent-ancestry tracking across changes, so it's a separate roadmap item.
  Limiting scope today = giving DB4AI an account (or token) that only sees the right folders.
- **Q: Item shape?** Type `document`, key = Drive file id (stable across renames and moves), title = file name,
  metadata `mime_type`, `modified_time`, `web_view_link`. Not owners/emails: personal data that would add erasure scope.
- **Q: Errors?** 429, 5xx and `rateLimitExceeded`/`userRateLimitExceeded` 403s retry with exponential backoff (4 tries,
  ≤32 s). A file whose export or download returns 403/404 (export disabled, deleted meanwhile) is skipped and logged
  rather than blocking the sync forever. Other failures raise `DriveError`; batches already applied stay applied.
- **Q: Deleted/trashed files?** Ignored in v1, per the next roadmap item ("Source deletions in connectors").
- **Q: Tests?** A fake Drive behind `httpx.MockTransport` (paged list and changes, export, media, token refresh); no
  network or Google account.
- **Not owner-level:** no new account or paid service; the owner opts in by supplying their own token. No migration.


## 2026-11-23 — source deletions in connectors
- **Q: How does a connector report a deletion?** `Batch.deleted`: a list of source keys next to `records`, saved in the
  same transaction as the batch and its cursor move (so a crash can't lose a deletion or replay a half). Over HTTP,
  `POST /sync/{source}` takes `deleted: [key, ...]`. Records plus deletions count toward the 500-per-batch limit.
- **Q: What happens to a deleted record's item?** The batch proposes `archive_item` through the harness (source
  `connector:<source>`, payload `{item_id, for_version, source_deleted: <source>}`). The tier comes from `policies`, so
  it is `ask_first` by default and fails closed; nothing is archived until a human approves. A team that trusts its
  source can set the policy to `auto_notice` itself. Never erase: deleting a file in Drive isn't an erasure request.
- **Q: Which items get a proposal?** Only an item with that external key in the batch's team whose current status is
  active or stale. Unknown keys, and archived, merged or erased items, are ignored. Re-sending a deletion is
  idempotent: the fingerprint finds the pending (or rejected) decision and makes no new one, so a rejected deletion
  ("keep it anyway") is never proposed again.
- **Q: What goes in the proposal?** Item id only, plus a reason "deleted in <source>". Not the source key or title:
  a key can be a file path, and harness rows shouldn't add erasure scope. The diff (title, status) is computed by the
  existing `archive_item` action as for any other proposal.
- **Q: The file comes back before anyone decides?** Saving a record for an item withdraws its pending
  source-deletion proposals from that source (status `expired`, note "withdrawn: back in <source>", change_log row),
  so approving later can't archive a live file. If the archive was already approved, the item stays archived
  (re-imports never revive lifecycle state, as before); a human can revert the decision.
- **Q: Drive?** A change with `removed: true` (deleted, or the account lost access) or a file now `trashed` becomes a
  deletion. Both mean DB4AI can no longer read it from the source, and a human decides either way. The first full
  listing reports no deletions.
- **Q: Transcripts folder?** Its mtime cursor can't see deletions, so connectors may offer `deleted(known_keys)`, a
  full-listing check that `connectors.run` calls after the batches. `known_keys` = the source's live items in the
  team, minus those with a pending or rejected source-deletion proposal (so an idle run writes nothing). Fail closed:
  a missing folder is an error, and a folder with no transcripts at all reports no deletions (an unmounted share
  would otherwise propose archiving everything).
- **Not owner-level:** no data is deleted or migrated (archiving is a reversible new version behind approval), no
  external service, no migration.


## 2026-11-23 — Drive folder scoping
Split from "Drive folder scoping and more file types": this run does scoping; file types is the next roadmap item.
- **Q: How does the owner choose the scope?** `db4ai sync gdrive --folder <id>` (repeatable), else
  `DB4AI_GDRIVE_FOLDERS` (comma-separated). Ids, not paths: names aren't unique in Drive and ids survive renames.
  A shared drive id is also its root folder id, so the same option covers shared drives. Empty = unscoped (v1 behaviour).
  Not stored in the DB: it's configuration next to the token, and a scope hash in the cursor detects changes.
- **Q: Subfolders?** Included at any depth. The in-scope folder tree is resolved by BFS over `files.list`
  (folder mimeType, `'<id>' in parents`, 40 parents per query) at the start of each sync, so it always reflects
  Drive now. More than 5000 folders is refused rather than half-synced. A file is in scope if any parent is in the tree.
- **Q: Fail closed?** A root that is missing, unreadable, trashed or not a folder is a `DriveError` before anything is
  written. A typo must not make every synced file look deleted. Ids containing quotes/backslashes are rejected (they go
  into Drive queries).
- **Q: What if the scope changes between syncs?** The cursor is prefixed `scope:<sha256(sorted ids)[:12]>:`. A cursor
  from another scope (or an unscoped one) starts a fresh full listing; re-sending is idempotent. Files no longer in
  scope are then caught by the deletion check below. The hash holds only folder ids, no content.
- **Q: Files moving out of scope?** A changed file of a synced type now outside the tree is reported as deleted
  (`archive_item`, ask_first); unknown keys are ignored by the framework, so files that were never synced cost nothing.
- **Q: Folders moving in or out?** The changes feed reports the folder, not its files. A changed folder inside the tree
  has its subtree listed (batches that don't move the cursor, so a crash replays them). For folders moving out,
  scoped syncs offer `deleted(known_keys)`: one metadata-only listing of the scope after the batches; synced live keys not
  in it are deletions. As with transcripts, a scope that lists no files at all reports no deletions. Cost: one listing
  per run, bounded by the scope the owner chose; unscoped syncs keep v1 behaviour and skip it.
- **Q: Resume mid-listing?** Listing cursor `list:<start>:<chunk>:<page>` over the sorted folder tree. If the tree
  changes between runs, the changes feed replays from `<start>` and the deletion check covers the rest.
- **Tests:** the fake Drive now filters `q` by mimeType and parents and serves `files.get`; a new test covers nested
  folders, moves in/out of files and folders, scope change, idempotent re-runs and fail-closed roots.
- **Not owner-level:** no data is deleted (out-of-scope files go through ask_first archive), no new service, no migration.


## 2026-11-23 — more Drive file types (PDF, .docx)
Split from "More Drive file types": this run does PDF and .docx; Sheets → datasets is a new roadmap item, because the
connector framework only saves items and a dataset needs its own record kind, table naming and schema-drift handling.
- **Q: How is text extracted?** With the CLI importer's code (`ingest.text_from_bytes`, pypdf pages / python-docx
  paragraphs), so a file synced from Drive and the same file imported by hand give the same body. Then the same
  normalisation as Docs (BOM, CRLF, strip).
- **Q: Store the original file as a blob?** No. The connector framework saves text records like `POST /items`; Drive
  remains the source of the file and `web_view_link` points back to it. A blob copy would add storage, orphan GC and
  erasure scope for no reader that needs it today.
- **Q: Unreadable files?** A corrupt or encrypted PDF/.docx is skipped and logged (exception type only, no content),
  like a 403 export, so one bad file can't block the sync forever. A file whose extracted text is empty (a scanned PDF;
  no OCR in v1) is skipped too, rather than saved as an empty item. A previously synced file that becomes unreadable
  keeps its last good version.
- **Q: Size limit?** The same 10 MB cap (`size` from Drive), checked before downloading.
- **Q: Existing installs already past their first listing?** Their `changes:` cursor would never see old PDFs. Every
  cursor now carries `types:<sha256 of sorted synced mime types>[:8]:` (after the scope tag); a cursor without the
  current tag starts one full listing, which is idempotent (unchanged files add no version). Cursors that aren't
  Drive cursors at all still fail with `DriveError`.
- **Q: Deletions/scope for the new types?** Automatic: they're in `SYNCED_TYPES`, which drives the listing query,
  trash/removal and out-of-scope checks.
- **Tests:** hand-built PDF (with and without a text layer), a broken PDF, python-docx files, an edit and a deletion,
  and an old untagged cursor re-listing once.
- **Not owner-level:** no new dependency (pypdf and python-docx were already required), no service, no migration.


## 2026-11-23 — dataset records in the connector framework
Split from "Drive Sheets as datasets": this run adds the dataset record kind to the framework (and `POST /sync`); the
Sheets export in `gdrive` is the next roadmap item.
- **Q: What does a connector send for a table?** A `DatasetRecord(key, name, columns, rows)`: the full current snapshot
  of one source table (a sheet tab), cells as strings. Snapshots, not row deltas, because Sheets (and most sources)
  have no stable row ids; DB4AI computes the delta.
- **Q: Which table does it go to?** The first snapshot of `<source>:<key>` creates `data.<team>_<name>` and records
  the key in a new `datasets.external_key` column (unique per team). If the name is taken (another sheet, a CSV
  import) a short hash of the key is appended. A later rename in the source keeps the table: BI dashboards point at it.
- **Q: How are rows updated?** Multiset diff in SQL over the synced columns (snapshot loaded into a temp table, rows
  matched with `is not distinct from` and row_number for duplicates): delete what's gone, insert what's new. Unchanged
  rows keep their `_row_id`, an identical snapshot writes nothing, and the existing capture trigger logs each real
  change in `change_log` with `connector:<source>` provenance. A changed cell is a delete + insert (no row identity).
- **Q: Schema drift?** New column, column gone from the source, or a value that no longer parses as the column's type
  → `alter_dataset` proposal (new policy row, `ask_first`), and that snapshot is not applied (fail closed: the table
  never mixes two schemas). The action only adds columns, widens to `text`, or marks columns `retired`; it never drops
  or narrows, so approving can't destroy data (no revert needed, none offered). Retired columns keep their values on
  existing rows and are null on new ones; a retired column that reappears is un-retired by the next `alter_dataset`.
  A newer drift expires the older pending proposal for that dataset; a rejected one is never re-proposed (the dataset
  stays as it was until the source or the policy changes). A team can set `alter_dataset` to `auto` to apply drift
  in the same batch.
- **Q: Deletions?** `deleted_datasets` → `archive_dataset` (new policy row, `ask_first`) on a live dataset; unknown or
  archived keys are ignored. Archiving never drops the table (BI keeps reading it); it sets `archived_at` and later
  snapshots are skipped. A snapshot that comes back withdraws a pending proposal; an approved archive can be reverted.
- **Q: Limits?** 200 columns, 100,000 rows per snapshot; a row longer than the header is rejected (cells would be
  silently lost). Each dataset record and deletion counts toward the 500 per batch.
- **Q: Erasure?** Synced datasets have no `item_id`, so item erasure doesn't touch them; erasing rows of a dataset is a
  separate future concern (noted, not needed for any current item).
- **Not owner-level:** no data deleted (no column drops, archive keeps the table), no service, one additive migration
  (`010_synced_datasets.sql`).


## 2026-11-23 — Drive Sheets as datasets
The connector half of the split item: `gdrive` now syncs Google Sheets tabs as dataset records.
- **Q: How is a tab read?** Sheets API v4 (`spreadsheets.get` for the tab list, then `values:batchGet` for the grid
  tabs), not the `docs.google.com/.../export?format=csv&gid=` URL: that URL isn't a documented API, and Drive's own
  `files.export` as CSV only gives the first tab. The `drive.readonly` token already covers Sheets reads, so no new
  scope or env var. Values are `FORMATTED_VALUE` (what a CSV export holds and what people see), so "$1,200" stays text
  and only plain numbers/ISO dates are typed; chart/object tabs are skipped.
- **Q: Key and name?** Key `<file id>#<sheetId>` (sheetId survives tab renames and reordering). Name on first sync:
  the file name when the spreadsheet has one grid tab, else `<file name> <tab title>`; later renames are ignored by the
  framework (BI keeps its table).
- **Q: Header and rows?** First row is the header; fully empty rows are dropped (blank spacer lines aren't data); a
  tab with no values is skipped (present, not deleted). The API trims trailing empty cells, so short rows are padded by
  the framework as nulls. A value to the right of the last header cell is still data: the header is padded to the
  widest row and those columns are named `column`, `column_2`...
- **Q: A tab the framework would reject (>200 columns, >100,000 rows, or a grid over 5M cells)?** Skipped and logged, keeping the last good snapshot, so one odd tab can't block the whole sync (same rule
  as an unreadable PDF). A spreadsheet the Sheets API refuses (403/404) is skipped likewise, and its tabs aren't
  reported deleted.
- **Q: How does the connector know which tabs it synced before (to report a removed tab or a removed file's tabs)?**
  New optional framework hook: `run` sets `connector.known_datasets` to the source's live dataset keys (no pending or
  rejected archive) before fetching, and the connector keeps it current as it emits. A removed/trashed/out-of-scope
  spreadsheet reports every known `<file id>#*` key in `deleted_datasets`; a changed spreadsheet reports its known tabs
  that are gone. A second optional hook, `deleted_datasets(known)`, runs after the batches like `deleted(known)`; scoped
  syncs use it (sharing one listing with `deleted`) for spreadsheets whose folder moved out.
- **Q: Batch size and crashes?** Each spreadsheet's tabs go in their own batch(es) (≤500 records) at the cursor the
  page started from, before the page's batch that moves the cursor; a crash replays the page, and snapshots are
  idempotent. One huge page can't build one giant transaction.
- **Q: Existing installs?** Adding the Sheets type changes the cursor `types:` tag, so existing cursors re-list once
  and pick up existing spreadsheets (idempotent for docs).
- **Not owner-level:** no data deleted (tab/file removals go through ask_first `archive_dataset`), no new service, no
  new account or scope, no migration.


## 2026-11-23 — `ask` audit log
Split from "`ask` follow-ups": this run adds the audit log; multi-turn context stays on the roadmap as its own item.
- **Q: On or off by default?** Off. The roadmap says "optional", and a question can itself be personal data ("what is
  X's salary?"). `DB4AI_ASK_LOG` = `off` (default) | `ids` | `text`. An unknown value makes `ask` raise rather than
  answer unlogged or log more than asked (fail closed on misconfiguration).
- **Q: What is stored?** One `ask_log` row per call: actor, time, `answered`, model, type filter, the `sources` and
  `citations` as `(item_id, version)` pairs (plus `source_items` / `cited_items` uuid arrays for lookups), and a sha256
  of the normalised question. `text` mode also stores the question. Never the answer, passages, titles or quotes: they
  are item content (rebuildable from the cited versions) and would widen erasure scope.
- **Q: Same transaction?** Yes. With logging on, the answer is computed and the row inserted in one transaction; if
  the insert fails, `ask` raises and returns nothing, so no answer is given without its trace.
- **Q: Append-only?** Yes (trigger). The one exception is erasure: while an `erase_item`/`redact_item` decision is
  applied, rows whose `source_items` contain the target may change `question` only. Erase nulls the question of those
  rows (it may have been about the erased content); redact replaces the terms in it. Ids and versions stay: they
  hold no content, like `notifications`.
- **Q: Who reads it?** `GET /ask/log` (`actor`, `item`, `limit` filters): admins see every row, anyone else only their
  own. No MCP tool: it is an audit surface for people, not for agents.
- **Q: Retention?** None in v1 (deleting rows would be a new bypass of append-only; the log is opt-in). Raise as a
  question if an owner turns it on and wants expiry.
- **Not owner-level:** opt-in, no service, no account, one additive migration (`011_ask_log.sql`).

## 2026-11-23 — `ask` multi-turn context
The other half of "`ask` follow-ups". Grilled one question at a time; each answer is the recommended one.
- **Q: Who holds the conversation, server or client?** The client. `ask` takes an optional `history` of earlier turns
  (`question`, `answer`, `citations` with `item_id`), oldest first. A server-side conversation table would store answers,
  which are item content, widening erasure scope again and adding a retention question; MCP clients (Claude) and HTTP
  callers already hold the conversation. `ask` stays a read.
- **Q: Is client-supplied history trusted?** No. It can be fabricated, so it is context, never evidence: history
  answers are never passages and can't be cited, the grounding rule (≥1 verbatim quote from a passage DB4AI retrieved)
  is unchanged, and Claude gets the history in its own tag with "earlier turns are not sources".
- **Q: How does retrieval use earlier turns?** Three sources, deduped by item, in order: (1) `search(question)` up to
  `limit`; (2) if that found fewer than `limit`, `search(previous question + question)` fills the rest (a plain
  concatenation can't replace (1): `plainto_tsquery` ANDs every term, so it would miss full-text hits); (3) the items
  cited in earlier turns, newest turn first, at most 5, as extra passages beyond `limit` so a vague follow-up ("who
  owns it?") still sees the item the conversation is about.
- **Q: Can carried item ids leak items?** No. They are re-read through the same filter as search (actor's teams unless
  admin, active only, type filter) at their current version; unknown, unreadable or inactive ids are dropped silently
  (no existence oracle). The passage is the item's chunk sharing the most terms with the question plus the previous one.
  A malformed id is a 400.
- **Q: How does the heuristic model use history?** Terms of the question first; if no sentence qualifies, it retries
  with the previous question's terms added. Claude resolves references itself from the history.
- **Q: Limits?** At most 10 turns (older ones are a 400, not silently cut, so the caller knows); each question ≤2000
  characters, each answer ≤4000, ≤20 citations per turn.
- **Q: Audit log?** One additive column `ask_log.history_turns` (0 for a first question). Earlier questions and answers
  are not stored: they were logged by their own call if made through DB4AI, and a client-supplied history may be
  invented. Carried items appear in `sources` like any passage, so erasure already finds those rows.
- **Not owner-level:** no new service, no stored content, one additive migration (`012_ask_history.sql`).

## 2026-11-24 — Per-team notification routing
Grilled one question at a time; each answer is the recommended one, resolved from the code.
- **Q: Where does the mapping live and what does it hold?** A `notify_routes` table (team, env var name, created by,
  created at), as the roadmap says: the URL is a secret and stays in the environment. The variable's value uses the same
  format as `DB4AI_NOTIFY_WEBHOOKS` (`slack:`/`teams:` prefix, comma-separated), so one parser serves both.
- **Q: Can a route name any env var?** No. Names must match `DB4AI_WEBHOOK_<A-Z0-9_>` (checked in code and by a check
  constraint). Otherwise an owner could point a route at `DB4AI_GDRIVE_TOKEN` or `DB4AI_DATABASE_URL` and have the
  worker POST to (and log errors about) a secret.
- **Q: What happens to the global webhooks?** Unchanged: they still hear about every decision. They act as the admin
  feed, existing installs behave the same, and admin-only decisions need somewhere to go. Team routes are additive.
- **Q: Do team channels hear about erasure/redaction?** No. Only admins can decide them, and even an id-only message
  "erase_item proposed for team eng" can reveal that someone filed a GDPR request. They go to global webhooks only.
  Decisions with no team (global scope) also go to global webhooks only.
- **Q: The same URL configured globally and for a team?** One webhook (id = hash of the URL, as before); its scope is
  the union, so it gets one message and `notifications` dedupe still holds.
- **Q: When is routing resolved?** Twice: at propose time to decide which jobs to queue (in the proposing transaction,
  as before), and at send time to decide which decisions to name. So removing a route or unsetting a variable before
  the job runs sends nothing, like removing a global webhook did. An unset variable is logged as a warning, not an
  error: notifications aren't a safety gate (decisions stay pending and visible in `/harness`), so nothing fails open.
- **Q: Who manages routes?** Admins and owners of the team, matching `set_policy`. Members (and only readers of the
  team) can list them; listings say whether the variable is set, never its value. Each add/remove is a `change_log`
  row (entity `notify_route`, env var name only); removal deletes the config row. Surfaces: HTTP
  (`GET`/`PUT`/`DELETE /teams/{team}/notify-routes[/{env_name}]`) and CLI (`db4ai notify-route add|remove|list --as`).
  No MCP tool: this is operator configuration, not something an agent should change. The CLI needs `--as` rather than
  inventing an admin identity.
- **Not owner-level:** no data deleted, no new service or account, one additive migration (`013_notify_routes.sql`).

## 2026-11-24 — "As of" queries
Grilled one question at a time; each answer is the recommended one, resolved from the code.
- **Q: What does "the item as of T" mean?** Its highest-numbered version with `created_at <= T`. `created_at` is
  `clock_timestamp()` (row insert time), so versions in one transaction still order. A version whose transaction
  committed after T but inserted before it counts as "then"; acceptable for an audit read, noted here.
- **Q: Which surfaces?** `as_of` on `get_item` and `search` (HTTP query param, MCP argument, `service`). `history`
  already shows every version. `ask` as of a time is split into its own roadmap item to keep this run to one change.
- **Q: Item created after T, or unknown?** Both "not found" (404). Team read access is checked first, against the
  item's team (teams never change per version), so outsiders get 403 as today.
- **Q: Links?** Links can be deleted (undoing a link decision), so `links` is rebuilt from `change_log`: link inserts
  at or before T minus link deletes at or before T, with the other item's title as of T; links to items that didn't
  exist yet are dropped.
- **Q: Search as of T?** Full-text only, over each item's version as of T; status filter (`active` unless
  `include_inactive`) applies to the status then. Vector scores are skipped: `item_chunks`/`item_embeddings` are
  derived copies of current content, so an old version has none, and re-embedding the past per query is too costly.
  Results carry `version` and `chunk: null`; the score is the same full-text half as live search.
- **Q: Erased items?** Their old versions are scrubbed in place, so an as-of read returns the tombstone text with
  `current_status: erased`; as-of search excludes items erased since. Erasure stays complete.
- **Q: Timestamp format?** ISO 8601; `Z` accepted; no offset means UTC; anything else is a 400.
- **Q: Index/migration?** None. Item lookups use `unique (item_id, version)`; as-of search scans versions up to T,
  fine for a rare audit query. Add an index if it shows up in practice.
- **Not owner-level:** read-only, no migration, no new service.

## 2026-11-24 — `ask` as of a time
Grilled one question at a time; each answer is the recommended one, resolved from the code.
- **Q: How does `ask` retrieve as of T?** With `store.search_as_of` (full-text over each item's version at or before T),
  for both the question and the follow-up retry with the previous question. Vector scores stay skipped, as for as-of
  search: chunks and embeddings only exist for current content.
- **Q: Which text is the passage?** The best-matching window of the version that matched (fetched by
  `(item_id, version)`), never the current body. Citations carry that old version, and the verbatim-quote check runs
  against the old text, so a quote from today's text can't ground an answer about then.
- **Q: History-carried items?** Re-read as of T under the same rules as as-of search: readable team, version then
  `active`, type filter, and items erased since dropped. Unknown or unreadable ids drop silently, as for live follow-ups.
- **Q: Erasure?** Items erased since never come back (search excludes them; carried items too). Erasure stays complete.
- **Q: Audit?** Additive column `ask_log.as_of` (null for live questions), so the log says which point in time was read;
  the `(item_id, version)` pairs already pin the exact text. `GET /ask/log` returns it.
- **Q: Result shape?** Unchanged, plus `as_of` echoed when given. Bad timestamps are a 400 via `parse_as_of`.
- **Surfaces:** `POST /ask` body `as_of`, MCP `ask(as_of=...)`, `service.ask`. No CLI `ask` exists, so none added.
- **Not owner-level:** read-only, one additive migration (`014_ask_as_of.sql`), no new service.

## 2026-11-24 — Per-item ACLs beyond team scope
Grilled one question at a time; each answer is the recommended one, resolved from the code.
- **Q: Narrow, widen, or both?** Narrow first: an item can be restricted to named readers inside its team. Narrowing
  fails closed (a bug hides too much, never too little) and needs no cross-team rules. Sharing with other teams is
  split into its own roadmap item.
- **Q: Who always reads a restricted item?** Its listed readers, the team's owners and admins. Owners decide the
  team's harness decisions, whose diffs show item content, so hiding items from them would either leak via diffs or
  block their decisions. Readers must be team members (a 400 otherwise): the ACL is an intersection with team scope.
- **Q: Where is it stored?** `item_acl (item_id, readers uuid[], set_by, decision_id, set_at)`, current state like
  `team_members`, with every change in `change_log` (entity `item_acl`, ids only). ACLs hold no content, so erasure
  has nothing to scrub. Not versioned with the item: access is not content, and a content revert must not change it.
- **Q: How is it enforced?** One SQL function `db4ai_item_visible(item, actor)` used by every read query (search,
  vector search, as-of search, `ask` carried items, links, harness listings via `db4ai_decision_visible(payload)`), and
  `store.require_item` for single-item reads and writes. A hidden item is "not found" (404), like an unknown id, so
  ids can't be probed; outsiders to the team still get 403 as before.
- **Q: How does access change?** A new item can be created restricted (`readers` on `POST /items`, MCP
  `save_document`; the creator is always added), so a sensitive item is never visible to the whole team first. Changes
  afterwards are a `set_item_access` decision: always `ask_first` (hard-coded like erasure, since lifting widens access),
  decided by owners/admins, revertible. Each request carries a request id, so restrict → lift → restrict again isn't
  swallowed by the harness's "never re-propose a fingerprint" rule. `readers: []` leaves only owners and admins.
  Passing `readers` for an existing item is a 400 (use the request).
- **Q: Maintainers?** The maintainer agent is not a reader. Enrichment and embeddings still run (they stay on the item
  and its readers' search), but `relate` skips restricted items and never picks one as a neighbour, so no link, merge,
  alias or staleness explanation carries their id or text to another item. Schema promotion skips them (`data.*`
  tables are team-wide). `merge_items` refuses items with different readers.
- **Q: Writes?** Need read access: an upsert by key, revert, erasure or access request on a hidden item fails. A
  connector whose actor isn't a reader can't update a restricted item; add it as a reader. Sync batches don't set
  readers in v1.
- **Q: As-of reads?** Today's ACL, like today's team scope.
- **Q: Notifications?** Unchanged: messages carry ids, action types and counts, no content.
- **Not owner-level:** no data deleted, additive migration (`015_item_acl.sql`), no new service.

## 2026-11-24 — Sharing an item with another team
Grilled one question at a time; each answer is the recommended one, resolved from the code.
- **Q: What does a share grant?** Read only: get, history, as-of reads, search, as-of search, `ask` (and its carried
  history), and the item as a link's other end. Writes (upsert by key, revert), access/share/erasure requests, the
  access listing, erasure records and harness decisions stay with the item's team: a sharee is a reader, not a member.
- **Q: Grant to whom?** Other teams (dynamic: whoever is a member now) and individual actors outside the item's team.
  An actor who is already a member of the item's team is a 400 ("use readers"), and so is the item's own team, so
  readers (narrowing inside the team) and shares (widening outside it) stay two separate mechanisms.
- **Q: Storage?** `item_shares (item_id, teams uuid[], actors uuid[], set_by, decision_id, set_at)`, current state like
  `item_acl`, every change in `change_log` (entity `item_shares`, ids only). No row = not shared. Holds no content.
- **Q: How does it change?** Only via a `set_item_shares` decision: always `ask_first` (it widens access), scoped to the
  item's team so its owners or admins decide, revertible, each request its own decision (request id). The request
  carries the full new list; empty lists unshare. No "share at creation" in v1: widening should always be reviewed.
- **Q: Restricted items?** A share is an explicit owner-approved grant, so it applies even when the item has readers
  (a member of a grantee team reads it whatever `item_acl` says). Documented, not refused.
- **Q: Enforcement?** SQL `db4ai_item_shared(item, actor)`; every read query's scope becomes
  "(own team and `db4ai_item_visible`) or shared". `store.require_item` gains `shared=False` (fail closed): only the
  read paths listed above pass `shared=True`. An outsider without a share still gets 403, as before.
- **Q: Maintainers?** Shared items are skipped by `relate` and never picked as neighbours (like restricted items):
  a link, merge alias or staleness explanation would carry another item's id or text to the sharees. `merge_items`
  refuses items with different shares. Enrichment and embeddings still run. Schema promotion is unchanged (`data.*`
  tables are the item's own team's).
- **Q: What does a sharee see of the item's access?** `get_item` omits `readers` and `shared_with` for sharees (names
  of the other team's members); members see both. `GET /items/{id}/access` adds `shared_with` (team only).
- **Q: As-of reads?** Today's shares, like today's ACL.
- **Surfaces:** `POST /items/{id}/shares` (`{"teams": [...], "actors": [...], "reason": ...}`), `service.propose_shares`.
  No MCP or CLI: changing access is a human request over HTTP, as for readers.
- **Not owner-level:** no data deleted, additive migration (`016_item_shares.sql`), no new service.

## 2026-11-24 — Evaluation set for the maintainers
Grilled one question at a time; each answer is the recommended one, resolved from the code.
- **Q: What is measured?** The two judgements that propose changes to other items: dedup (`merge_items`) and
  staleness (`mark_stale`). Each eval case is a labelled pair (older item, newer item) and the metric is precision
  (and recall, F1) of "would `relate` propose this for the pair?". Linking is left out: a wrong link is cheap.
- **Q: Does the eval call the same code as `relate`?** Yes. The pair rules are pulled out of `relate` into
  `maintainers.duplicate_pair` and `maintainers.staleness_pair`, and the eval embeds `title\nbody` exactly as `enrich`
  does, then runs `llm.contradiction` with the same old/new texts. A pair that is a duplicate is not checked for
  staleness, as in `relate`. So tuning against the eval tunes production.
- **Q: Does it need Postgres?** No. It runs pairs in memory and writes nothing: an eval must never propose, merge or
  mark anything in a real instance. That also makes it runnable anywhere the package is installed.
- **Q: Where does the set live?** `db4ai/evals/maintainers.json`, shipped as package data: synthetic, hand-labelled
  cases (no company data), each with an id, task, both items (type, title, body), the label and a one-line note on what
  makes it hard (hard negatives: same template for another customer, a topic update with no changed fact). An owner can
  pass their own file with `--cases` (same format), so labelled pairs from real data stay on their machine.
- **Q: How is the real Claude path used?** `db4ai eval` uses the configured LLM and embedder (`get_llm`,
  `get_embedder`), so with `ANTHROPIC_API_KEY` set it measures Claude; `--llm heuristic|claude` overrides. Claude calls
  are made only when the owner runs the command; tests use the heuristic + hash embedder and never call the network.
- **Q: Output and gating?** JSON: per task tp/fp/fn/tn, precision, recall, F1, the thresholds used, and each miss
  (case id, expected, predicted, similarity, explanation). `--min-precision X` exits 1 if any task scores below X, so
  it can gate a model or threshold change in CI. `--dup-threshold` overrides `DB4AI_DUP_THRESHOLD` to try a value.
- **Q: Regression tests?** The set's shape is checked (unique ids, known tasks, both labels per task) and the heuristic
  baseline is pinned as a floor, so a change that makes the offline maintainers worse fails the suite.
- **Not owner-level:** read-only, no migration, no new service; Claude usage is opt-in and on the owner's own key.

## 2026-11-24 — Heuristic staleness precision
Grilled one question at a time; each answer is the recommended one, resolved from the code.
- **Q: What is a fact's "subject" offline?** The capitalised names the heuristic already extracts for enrichment
  (project, customer, product, person), split into lowercase words. No new model or dependency.
- **Q: When does a missing shared subject block a contradiction?** Only when *both* texts name something and the
  name sets are disjoint. If either side names nothing ("Support SLA", "platform team"), the old rule stands, so
  recall on unnamed facts is unchanged. Sharing one word (e.g. "Atlas" in "Atlas release") is enough.
- **Q: Months and weekdays?** Excluded from subjects: they are the facts that change ("March 3" → "April 14"), so
  counting them as subjects would suppress exactly the contradictions we want. New eval case `stale-month-moved`.
- **Q: Sentence-start names?** `_CAP` matched "The Atlas" as one name at a line start, and the old start check never
  fired after a newline (it compared the separator's start, not the next word's). Now only the sentence-initial word
  is dropped ("The Atlas" → "Atlas"). This also changes `enrich` entities, for the better (no "The Atlas"); the
  suite is unchanged otherwise.
- **Q: Does the Claude path change?** No: its prompt already says topic overlap alone isn't a contradiction. This is
  the offline fallback that `auto_notice` stale marks use without review.
- **Q: Floors?** Staleness precision 0.8 → 1.0, recall 0.57 → 0.62 (new positive case caught), with
  `stale-other-subject`, new hard negative `stale-other-customer` and `stale-month-moved` pinned as non-misses. The
  CLI gate test now forces a failure with `--dup-threshold 0.3`, since both tasks score 1.0 at the defaults.
- **Not owner-level:** heuristic-only, no migration, no data touched.

## 2026-11-24 — Notices for `auto_notice` changes
The roadmap was fully ticked; this run added three items (above) and took the first. Grilled one question at a time;
each answer is the recommended one, resolved from the code.
- **Q: Which decisions get a notice?** Those applied with tier `auto_notice` (status `applied_auto`). Not `auto`:
  every enrichment would page someone, and `auto` makes no revert promise. A team that sets `add_tags` to
  `auto_notice` opts into notices for it.
- **Q: A new job kind or the existing one?** The existing `notify` job: `propose` queues it in the applying transaction
  (same dedupe per webhook and delay), so a burst of proposals and auto-applied changes is one message.
- **Q: What is named at send time?** Pending, unexpired decisions as before, plus `auto_notice` decisions still
  `applied_auto` whose `revert_until` is in the future and that weren't sent to that webhook. A change reverted (or
  out of its window) before the job runs is not named: there is nothing left to act on.
- **Q: Message shape?** Two sections: "waiting for review" and "applied automatically, revert by <date>", each line
  with action, team, confidence, proposer and decision id; the `MAX_LISTED` cap covers both. The review link points at
  `/harness?status=applied_auto` for notices. No item content, as before.
- **Q: Routing?** Unchanged: global webhooks hear everything, team routes hear their team's (`mark_stale` and
  `merge_items` aren't admin-only).
- **Q: Recording?** The same `notifications` row (decision id, webhook hash) after a successful POST; no migration.
- **Q: Existing auto-applied decisions at upgrade?** Those still inside their window are named in the next message to
  each webhook (the listing cap keeps it short). They are still revertible, so hearing about them is useful.
- **Not owner-level:** no data deleted, no migration, no new service or account.

## 2026-11-25 — Readers on synced records
Grilled one question at a time; each answer is the recommended one, resolved from the code.
- **Q: Scope of this run?** The connector framework (`Record.readers`, `POST /sync/{source}` items' `readers`). Mapping
  Drive permissions is split into its own roadmap item: actors have only a `name`, so Drive's emails need a mapping
  first.
- **Q: Names or ids?** Actor names, as on `POST /items`.
- **Q: A reader who isn't a team member (unknown, or another team)?** Dropped and reported (`readers_dropped`); the item
  is still restricted to the rest. Raising would fail a whole batch on one stray Drive collaborator, and dropping only
  narrows access (fail closed). Widening to other teams is sharing, which stays an owner request.
- **Q: Is the syncer a reader?** Yes, as `save_item` already does for creators: otherwise the next sync of the item is a
  403 and the batch fails.
- **Q: Readers on an item that already exists?** Changing access is always `ask_first` (SPEC §8), so a differing set
  proposes `set_item_access` in the batch's transaction instead of applying it. The record's content is still saved.
- **Q: Idempotence?** The payload carries `source_readers` and `acl_changes` (count of the item's `item_acl` change_log
  rows): a re-sent batch has the same fingerprint (skipped), a rejected change isn't re-proposed, and after any ACL
  change a new difference is a new decision. A newer set from the source, or one that matches the ACL again, withdraws
  the pending one (`expired`), like `alter_dataset` supersession.
- **Q: `readers` = None?** The source says nothing: never changes access, and never lifts a restriction. Lifting from
  a sync would need an explicit "public" signal; not needed until a connector has one.
- **Not owner-level:** no migration, no data deleted, no new service.
