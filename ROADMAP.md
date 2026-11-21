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
- [ ] **One version per enrichment.** Combine summary, tags and entities into a single `enrich` action and
      version. Why: each enrichment adds 3 versions today.
- [ ] **Redaction and erasure via the AIDBHarness.** Always `ask_first` and admin-approved. Scrub the content
      from every version and blob while keeping a tombstone saying what was erased, by whom and why.
      This is the only sanctioned bypass of the append-only triggers. Why: GDPR and legal erasure requests.
- [ ] **`ask` tool.** Claude asks a question and gets an answer grounded in DB4AI items, citing item ids and
      versions (MCP + HTTP). Why: storing and searching isn't answering.

## Next: get real data flowing in

- [ ] Notifications for pending harness decisions (Slack/Teams webhook, via an outbox job).
- [ ] Connector framework on top of `POST /items` (incremental sync cursor per source), then the first
      connectors: Google Drive/Docs, meeting transcripts.
- [ ] "As of" queries: read any item or search results as they were at a given time.

## Later

- [ ] Per-item ACLs beyond team scope.
- [ ] Evaluation set for the maintainers (dedup, staleness precision) for use with the real Claude path.
