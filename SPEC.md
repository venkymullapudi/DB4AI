# DB4AI v1 — Spec

Decisions reached by grilling (one question at a time). Each line is a locked decision.

## 1. Shape
- One self-hosted instance per company. The company owns it; it mostly maintains itself.
- Holds **structured** data (analytics tables queryable by BI tools such as Tableau) and
  **unstructured** data (meetings, documents, whiteboards, team memory) side by side.
- When a human is needed, the system asks through the **AIDBHarness** (§5).

## 2. Storage & ACID
- **Postgres is the only system of record.** Items, versions, embeddings, links, structured
  tables, harness decisions and the job queue all live in it, so one transaction covers all of them.
- **Blobs** (recordings, images, PDFs) go to a content-addressed blob store (local dir or S3/MinIO).
  The blob is written *first*, under its sha256; then the row that points at it commits.
  A failed commit leaves an orphan blob, which the hygiene job collects. A row never points at a missing blob.
- Any other store added later (search engine, OLAP copy) is a **derived copy fed by the outbox**
  (the `jobs` table) and must be rebuildable from Postgres.
- Long items are split into **chunks** (paragraphs packed up to `DB4AI_CHUNK_CHARS`) and each chunk is
  embedded in `item_chunks`. Chunks store offsets into the body plus the `content_hash` they were cut from,
  not a copy of the text; search ignores chunks from old content and returns each item's best chunk.
  A whole-item vector stays in `item_embeddings` for linking and dedup.
- Embeddings use **pgvector** when the extension is available, otherwise a `float8[]` column
  with a SQL cosine function (same API).
- Each embedding model has **fixed dimensions**, recorded in `embedding_models` on first use; a vector of
  another size is rejected. With pgvector every model gets a partial HNSW index (cosine) per embeddings table on
  `embedding::vector(dims)`. Vector search takes the `DB4AI_VECTOR_CANDIDATES` nearest rows from the index, then
  applies team, type and status filters; if fewer results than asked survive, it reruns as an exact scan, so
  filters never hide matches. The `float8[]` fallback runs the same queries without an index.

## 3. Data model — append-only with provenance
- Every unstructured thing is an `item` (stable id, team, type) with a chain of `item_versions`.
  Versions are **never updated or deleted** (enforced by trigger). Current state = latest version.
- `item_heads` points each item at its latest version. A trigger advances it in the same transaction as the
  version insert; it can only move forward and can't be deleted. `items_current` reads through it, so reads
  don't scan the version history.
- Each version records `actor`, `source`, `reason`, and `harness_decision_id` if a human approved it.
- Status (active / archived / merged) is part of the version, so archiving and merging are also just new versions.
- Revert = new version that copies an earlier one.
- **"As of" reads** (`as_of`, ISO 8601; no offset = UTC): an item reads as its latest version created at or before
  then, plus `current_version`/`current_status`; links are rebuilt from `change_log` (inserts minus deletes up to
  then), with the other item's title as of then. An item that didn't exist yet is "not found"; team scope is today's.
  As-of search is full-text over each item's version then (status filters apply to the status then); vector scores
  are skipped because chunks and embeddings are derived from current content only. Items erased since are left out of
  as-of search, and their old versions read back scrubbed. Reads change nothing and aren't logged, like `get_item`.
- **Erasure / redaction** (`erase_item`, `redact_item`) is the only sanctioned bypass of the append-only triggers.
  Both are always `ask_first` (whatever `policies` says) and only an admin can approve them; neither can be reverted.
  While the approved decision is applied, the triggers let the content columns (title, body, metadata, blob,
  content hash, reason) of *that decision's item* change, plus the content of its `change_log` rows and of rows
  written under decisions that mention it. Ids, version numbers, actors, sources and timestamps never change, and
  rows are never deleted. Erase scrubs every version, adds a final `erased` version (which accepts no more writes),
  hashes the external key so a re-import is skipped, drops embeddings and chunks, and deletes blobs no other item
  uses (row in the transaction, file via a `blob_delete` outbox job). Redact replaces terms (case-insensitive) with
  `[redacted]` in the same places and re-enriches. Each writes an `erasures` tombstone (kind, counts, requester,
  approver, reason, data rows, copies) that never holds the erased content.
- **Copies** of the content outside the item: `data.*` rows with the item's id in `item_id` (e.g. a `promote_schema`
  backfill) are scrubbed in the same transaction (erase nulls their values, redact replaces terms), and so is their
  `change_log` history. Other items whose versions hold the erased text (redact: the terms; erase: body sentences of
  40+ characters, plus the item's title in items it was merged into) each get a `redact_item` proposal (ask_first,
  admin-only, at most 100 per erasure, source `erasure`, `copy_of` = the item), because a quote may be legitimate.
- Structured datasets are real tables in the `data` schema. A row-level trigger writes every
  change to the shared append-only `change_log` with the same actor/source/reason fields.
- **Synced datasets** (connector snapshots, §6) have `datasets.external_key` = `<source>:<key>`. Their columns can be
  marked `retired` (no longer in the source; kept with their values, null on new rows) but are never dropped, and an
  archived dataset (`archived_at`) keeps its table and stops syncing.

## 4. Self-maintenance — six maintainer jobs
1. **enrich** — summary, tags, entities, embedding for new/changed content. Summary, tags and entities are
   applied by one `enrich` action, so each enrichment is one harness decision and one new version.
2. **link** — connect related items (embedding similarity + shared entities, Obsidian wikilinks).
3. **dedup** — near-duplicate items → propose merge.
4. **staleness** — newer items contradicting older memories → propose mark-stale. The offline heuristic needs the
   same topic and a changed date or number, and refuses when both items name something (project, customer) but
   name nothing in common; month and weekday names are facts, not subjects.
5. **schema** — recurring metadata shape across items of one type → propose promoting it to a table.
6. **hygiene** — orphan blob GC, embedding rebuilds, expiring harness decisions.

- **Evaluation** (`db4ai eval`): labelled (old, new) pairs for dedup and staleness, scored in memory with the pair
  rules `relate` uses (`duplicate_pair`, `staleness_pair`; a duplicate pair is never checked for staleness) and the
  configured LLM and embedder. Reports precision/recall/F1 per task and every miss; writes nothing and needs no
  database. The built-in set (`db4ai/evals/maintainers.json`) is synthetic; `--cases` takes an owner's own file.
  The tests pin the heuristic baseline as a floor.

### Risk tiers (policy-driven, fail closed)
| tier | behaviour | default action types |
|---|---|---|
| `auto` | apply now, logged | `enrich`, `add_tags`, `add_link`, `set_summary`, `set_entities` |
| `auto_notice` | apply now; a human can revert within N days | `mark_stale`, `merge_items` (confidence ≥ threshold) |
| `ask_first` | queued in the harness; nothing happens until approved | `archive_item`, `promote_schema`, `alter_dataset`, `archive_dataset`, `merge_items` (low confidence), `resolve_contradiction`, `change_access`, `set_item_access` (always), `erase_item` and `redact_item` (always; admin-only), **any unknown action** |

Tiers come from the `policies` table (per team, with a global default). The AI never picks a tier.

## 5. AIDBHarness
- `harness_decisions` table: action, payload, diff, evidence, confidence, tier, scope team,
  status, expiry, decider, note, fingerprint.
- **Approve applies the change in the same transaction** that records the decision.
  Expired decisions are **not** applied. Rejections are remembered (fingerprint), so the same proposal isn't made again.
- Surfaces: MCP tools (`harness_list_pending`, `harness_decide`), a minimal web page (`/harness`),
  and the HTTP API.
- **Notifications** (Slack/Teams incoming webhooks, `DB4AI_NOTIFY_WEBHOOKS`): a new pending decision queues one
  `notify` outbox job per webhook in the proposing transaction (deduped per webhook, delayed
  `DB4AI_NOTIFY_DELAY_SECONDS` so a burst becomes one message). The job names the decisions still pending, unexpired
  and not yet sent to that webhook, and records them in `notifications` (ids only) after the POST succeeds; a failed
  POST retries with the queue's backoff (at-least-once). Messages carry action type, team, confidence, proposer,
  expiry, decision id and a review link (`DB4AI_PUBLIC_URL`), never item content, reasons or diffs, since a chat
  message can't be erased later.
- **Per-team routing**: `notify_routes` maps a team to the *name* of an env var (`DB4AI_WEBHOOK_<NAME>`, enforced by a
  check constraint) that holds its webhook URL(s) in the same format; URLs never enter the database. Global webhooks
  (`DB4AI_NOTIFY_WEBHOOKS`) still hear about every decision; a team route hears about its team's decisions except
  admin-only actions (`erase_item`, `redact_item`). Webhooks are identified by a hash of the URL, so a URL that is both
  global and routed gets one message. Routing is resolved when the decision is proposed (which jobs to queue) and again
  at send time (which decisions to name); an unset variable is logged and skipped. Admins and owners of the team add or
  remove routes (`PUT`/`DELETE /teams/{team}/notify-routes/{env_name}`, `db4ai notify-route`); members read them
  (`GET`, showing whether the variable is set, never its value). Every change is in `change_log`.

## 6. Ingestion (push-based)
- MCP write tools: `save_meeting`, `save_document`, `remember`, `log_decision`, `search`, `get_item`, `history`, `revert`,
  `request_erasure`, `request_redaction`.
- **`ask`** (MCP tool and `POST /ask`): answers a question from the items the actor can read. It retrieves with search
  (team access, active items only, best chunk per item; full-text-only hits use their best-matching chunk), gives the
  numbered passages to the LLM (heuristic: extractive sentences), and returns `answered`, `answer`, `citations`
  (item id, version, title, quote) and the `sources` searched. Citations to passages it wasn't given are dropped;
  a quote not found verbatim in its passage is kept as `null`. Without at least one verbatim quote the answer is not
  returned (`answered: false`, fail closed). No versions or decisions are written.
- **Follow-ups**: `ask` takes an optional `history` (earlier turns, oldest first: `question`, `answer`, `citations` with
  `item_id`; ≤10 turns, questions ≤2000 and answers ≤4000 characters, ≤20 citations per turn, else a 400). The server
  stores no conversation. History is context, never evidence: if the question alone finds fewer than `limit` items,
  search is rerun with the previous question prepended to fill the rest, and up to 5 items cited in earlier turns
  (newest first) are added as passages after re-reading them under the same access, active-only and type filters as
  search (unreadable or unknown ids are dropped silently). The model sees the earlier turns marked as not citable;
  the grounding rule is unchanged. `ask_log.history_turns` records how many turns were supplied, not their text.
- **As of a time**: `ask` takes `as_of` (same format as §"As of" reads). Retrieval is the as-of full-text search
  (each item's version at or before then, status filter applied to the status then, items erased since left out);
  passages, carried history items and citations all use those versions, so a quote must be in the old text. The
  result echoes `as_of`; `ask_log.as_of` records it (null for live questions).
- **`ask` audit log** (opt-in, `DB4AI_ASK_LOG` = `off` default | `ids` | `text`; any other value makes `ask` fail):
  each call writes one append-only `ask_log` row in the answer's transaction (a failed write returns no answer):
  actor, time, `answered`, model, type filter, sources and citations as `(item_id, version)` pairs, and a sha256 of
  the normalised question; `text` mode also keeps the question. Never the answer, quotes or titles. Erasing an item
  nulls the question of rows that searched it; redacting replaces the terms in it (the only update the log allows).
  `GET /ask/log` (`actor`, `item`, `limit`): admins see every row, others only their own.
- CLI import of files/folders: md, txt, pdf, docx → items; csv → structured dataset.
- Memory repo import: Markdown/Obsidian vault (frontmatter → metadata, `[[wikilinks]]` → links);
  re-import only adds versions for changed files.
- `POST /items` HTTP endpoint; `POST /items/{id}/erase`, `POST /items/{id}/redact` and `GET /items/{id}/erasures`.
- **Connectors** (incremental sync): a connector yields batches of records (key, type, title, body, metadata) and the
  cursor reached after each. A batch is saved like `POST /items` (external key `<source>:<key>`, provenance source
  `connector:<source>`) and the `sync_cursors` row for (source, team) moves in the same transaction, compare-and-set
  on the previous cursor (mismatch → 409 / `CursorConflict`, nothing written). At most 500 records per batch; every
  cursor move is in `change_log`. Cursors are opaque and never hold content. Re-sending is idempotent, and an erased
  item's key is withheld, so a sync can't bring it back. Surfaces: `POST /sync/{source}`, `GET /sync/{source}/cursor`,
  `db4ai sync <connector> <path>`.
- **Source deletions**: a batch can also carry `deleted` keys (counted toward the 500). Each one whose item in the team
  is active or stale gets an `archive_item` proposal in the same transaction (source `connector:<source>`, payload
  item id, `for_version` and `source_deleted`; no key or title), tiered by `policies` (`ask_first` by default). Unknown,
  archived, merged and erased items are ignored; re-sending is idempotent and a rejected deletion isn't re-proposed.
  Saving a record withdraws its pending deletion proposals from that source (status `expired`); an approved archive
  stays archived. Connectors whose cursor can't see deletions offer `deleted(known_keys)`, checked by `connectors.run`
  after the batches against the live keys that have no pending or rejected deletion. Built in:
  `transcripts` (a folder of `.vtt`/`.srt`/`.txt` → `meeting` items with `speakers`, `duration_seconds`, `path`;
  cursor = newest mtime synced, files at that mtime are re-sent; deletions by comparing the folder with the live keys,
  failing closed: a missing folder is an error, an empty one reports no deletions).
- **Dataset records**: a batch can carry `datasets` (key, name, columns, rows of cell strings: a full snapshot of one
  source table, at most 200 columns and 100,000 rows) and `deleted_datasets`; each counts as one record toward the 500.
  The first snapshot of a key creates `data.<team>_<name>` with inferred column types (name taken → `_<hash of key>`
  suffix; later renames in the source are ignored). Later snapshots are diffed into the table as a multiset of rows over
  the synced columns: rows the source no longer has are deleted, new rows inserted, unchanged rows keep their `_row_id`,
  so an identical snapshot writes nothing and `change_log` holds only real row changes. **Schema drift** (a new column, a
  column gone from the source, or a value that no longer fits a column's type) proposes `alter_dataset` (ask_first by
  default; payload `add`, `retype` to text, `retire`) and the snapshot is not applied until the table matches (fail
  closed); a newer drift supersedes an older pending one (status `expired`), and a rejected drift is not re-proposed.
  `alter_dataset` never drops or narrows a column and can't be reverted. A deleted key proposes `archive_dataset`
  (ask_first; payload dataset id and `source_deleted`), withdrawn if the snapshot comes back first; approved, the table
  stays and later snapshots are ignored until the decision is reverted. Optional connector hooks: `run` sets
  `connector.known_datasets` (the source's live dataset keys with no pending/rejected archive) before fetching, and calls
  `connector.deleted_datasets(known)` after the batches, like `deleted`.
- Built in: `gdrive` (`db4ai sync gdrive --team <t>`): Google Docs (exported as `text/plain`), text/Markdown files,
  and PDF / .docx files (downloaded; text extracted as by the CLI importer, file not stored as a blob), ≤10 MB, that the
  token's account can read, shared drives included, as `document` items (key = Drive file id; metadata
  `mime_type`, `modified_time`, `web_view_link`). OAuth token from the environment (`DB4AI_GDRIVE_TOKEN`, or a refresh
  token + client id/secret); DB4AI never stores it. Cursor = `list:<start>:<page>` during the first full listing (start
  = changes token taken before listing), then `changes:<token>`; one batch per API page, an idle sync writes nothing.
  429/5xx/rate-limit 403s are retried with backoff; a file whose export Drive refuses (403/404) is skipped and logged.
  A PDF/.docx that can't be parsed (corrupt, encrypted) or has no text (scanned, no OCR) is skipped and logged.
  Every cursor carries `types:<hash of the synced mime types>:`, so a release that syncs more types re-lists once.
  A removed (deleted or no longer shared) or trashed file is a deletion; the first full listing reports none.
  **Folder scope** (`--folder <id>`, repeatable, or `DB4AI_GDRIVE_FOLDERS`; a shared drive id is its root folder): only
  files under those folders, at any depth, are synced. The in-scope folder tree is resolved from Drive at each sync
  (at most 5000 folders); a root that is missing, trashed or not a folder is an error (fail closed). The cursor is
  prefixed `scope:<hash of the folder ids>:` (before the types tag) and the listing cursor is `list:<start>:<chunk>:<page>`; a cursor from
  another scope starts a fresh full listing. A file moved out of scope is a deletion; a folder moved in has its
  existing files listed; after each scoped sync, synced files no longer anywhere in scope (e.g. their folder moved
  out) are deletions too, unless the scope lists no files at all.
  **Google Sheets** are synced as datasets, not items: each grid tab (chart tabs ignored) is read with the Sheets API
  (`values:batchGet`, `FORMATTED_VALUE`, i.e. what a CSV export holds) as a dataset record keyed `<file id>#<sheetId>`,
  named after the file (one grid tab) or `<file name> <tab title>`. First row = header, padded to the widest row
  (unnamed columns become `column`, `column_2`...); fully empty rows are dropped; an empty tab is skipped. A tab over the
  dataset limits (200 columns, 100,000 rows) or a 5M-cell grid is skipped and logged, keeping its last snapshot; a
  spreadsheet the Sheets API refuses (403/404) is skipped and nothing is reported deleted. Each spreadsheet's tabs go in
  their own batch(es) at the cursor its page started from, before the page's batch. A tab removed from a spreadsheet,
  and every tab of a removed/trashed/out-of-scope spreadsheet, is reported in `deleted_datasets` (→ `archive_dataset`).
- More connectors (Notion, Miro, Slack) build on the same framework.

## 7. Stack
Python ≥3.11, FastAPI, psycopg 3 with plain SQL and a small SQL migration runner, pgvector,
Postgres-table job queue (`FOR UPDATE SKIP LOCKED`), Claude (`claude-sonnet-5-5`) for
enrichment and proposals with a heuristic fallback when no API key is set, pluggable embeddings
(Voyage, or a local hashing embedder for offline use), local or S3 blobs, docker compose, pytest against real Postgres.

## 8. Access control
- Teams; actors are `human` or `agent`. Roles: `admin`, `owner`, `member` (per team), `agent`.
- Every item belongs to one team; reads and writes are limited to the actor's teams (admins see all).
- API tokens (stored hashed). Harness decisions are routed to owners of the scope team; admins can decide anything.
- **Per-item ACLs** (`item_acl`): an item may be restricted to a list of readers, who must be members of its team.
  It is then readable only by them, the team's owners and admins (access narrows, never widens). No row = the whole
  team. A new item can be created restricted (`readers` on `POST /items`; the creator is always added); after that,
  changing or lifting it is a `set_item_access` decision (always `ask_first`, revertible, each request its own
  decision). Every read path applies it through `db4ai_item_visible(item, actor)`: get/history/as-of reads (a hidden
  item is 404, like an unknown id), search, as-of search, `ask` and its carried history, links (a hidden other end is
  dropped), and harness listings and reverts (a decision is hidden if any item its payload names is). Writing to a
  restricted item (upsert by key, revert, erasure or access requests) needs read access. Maintainers still enrich and
  embed restricted items, but never link, merge or mark them stale against other items, and schema promotion leaves
  them out of `data.*` tables (readable by the whole team); a merge of items with different readers is refused.
  As-of reads use today's ACL.
- **Sharing** (`item_shares`): an item can also be shared, read-only, with other teams (whoever is a member now) and
  with actors outside its team; members of the item's team and the team itself are refused (use readers). A share is
  an owner-approved grant, so it applies even to a restricted item. It changes only through a `set_item_shares`
  decision (always `ask_first`, scope = the item's team, full new list, empty lists unshare, revertible, each request
  its own decision); every change is in `change_log`. Enforced by `db4ai_item_shared(item, actor)`: every read scope is
  "(own team and `db4ai_item_visible`) or shared", and `store.require_item(shared=True)` is passed only by get, history
  and as-of reads. So a sharee reads the item, finds it in search and as-of search, gets it cited by `ask` (and its
  carried history) and sees it as a link's other end; writes, access/share/erasure requests, the access listing,
  erasure records and harness decisions stay with the team (403 otherwise). `get_item` shows `readers`/`shared_with`
  only to the item's team. Maintainers skip shared items in `relate` and never pick them as neighbours, and
  `merge_items` refuses items shared differently. As-of reads use today's shares.
