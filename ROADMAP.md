# DB4AI roadmap

Worked top to bottom by the improvement loop: one item per run (see `LOOP.md`).
Mark an item `[x]` when it is merged to `main` with passing tests. Add new items you discover at the
right priority, with a one-line reason.

## Now: make the core trustworthy at company scale

- [x] **Current-version pointer.** Add an `item_heads` table (item_id → current version row), maintained in
      the same transaction as each new version, and use it instead of the `DISTINCT ON` scan in `items_current`.
      Why: every query scans all versions today.
- [x] **Chunked embeddings.** Split long items into chunks and embed each one; search returns the matching
      chunk. Why: one vector per 2-hour transcript can't find a paragraph.
- [x] **CI.** A GitHub Actions workflow that runs pytest against `pgvector/pgvector:pg16`, plus a
      `docker compose` smoke test. Why: Docker and pgvector paths are untested.
- [x] **Vector index.** Set fixed embedding dimensions per model so pgvector can use an HNSW index (keep the
      `float8[]` fallback) on `item_chunks` and `item_embeddings`. Needs pgvector in tests, so do it with or
      after CI. Why: chunk search is a sequential scan today.
- [x] **One version per enrichment.** Combine summary, tags and entities into a single `enrich` action and
      version. Why: each enrichment adds 3 versions today.
- [x] **Redaction and erasure via the AIDBHarness.** Always `ask_first` and admin-approved. Scrub the content
      from every version and blob while keeping a tombstone saying what was erased, by whom and why.
      This is the only sanctioned bypass of the append-only triggers. Why: GDPR and legal erasure requests.
- [x] **Erasure of copied content.** Erase/redact cover one item and the decisions, audit rows and links that
      mention it. Its text can also live in other places: merge aliases on the surviving item, rows backfilled into
      `data.*` tables by `promote_schema` (and their `change_log` rows), and other items that quote it. Find and
      offer to scrub those (e.g. propose `redact_item` on each match). Why: a complete erasure answer for GDPR.
- [x] **`ask` tool.** Claude asks a question and gets an answer grounded in DB4AI items, citing item ids and
      versions (MCP + HTTP). Why: storing and searching isn't answering.

## Next: get real data flowing in

- [x] Notifications for pending harness decisions (Slack/Teams webhook, via an outbox job).
- [x] Connector framework on top of `POST /items` (incremental sync cursor per source), plus the first connector:
      meeting transcripts from a folder (.vtt/.srt/.txt).
- [x] **Google Drive/Docs connector** on the connector framework (Drive changes API page token as the cursor,
      Docs exported as text, OAuth token supplied by the owner via env). Why: split from the connector item.
- [ ] **Source deletions in connectors.** A record the source deleted proposes `archive_item` (ask_first) instead of
      being ignored. Why: v1 sync only upserts, so deleted Drive files would live on.
- [ ] **Drive folder scoping and more file types.** Limit `gdrive` to chosen folders/shared drives, and sync PDFs,
      .docx and Sheets (as CSV → structured datasets). Why: v1 syncs every Doc/text file the token can read.
- [ ] **`ask` follow-ups.** Optional audit log of questions asked (who asked what, which items were cited), and
      multi-turn context. Why: v1 `ask` is stateless and leaves no trace of what was read.
- [ ] **Per-team notification routing.** Send a team's pending decisions to that team's channel (webhook URLs are
      secrets, so map team → env name, not URL, in the DB). Why: v1 sends every decision to every webhook.
- [ ] "As of" queries: read any item or search results as they were at a given time.

## Later

- [ ] Per-item ACLs beyond team scope.
- [ ] Evaluation set for the maintainers (dedup, staleness precision) for use with the real Claude path.
