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
