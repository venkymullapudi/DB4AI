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
- [x] **Source deletions in connectors.** A record the source deleted proposes `archive_item` (ask_first) instead of
      being ignored. Why: v1 sync only upserts, so deleted Drive files would live on.
- [x] **Drive folder scoping.** Limit `gdrive` to chosen folders/shared drives. Why: v1 syncs every Doc/text file
      the token can read.
- [x] **More Drive file types.** Sync PDFs and .docx (text via the existing importers). Why: split from folder
      scoping; v1 only synced Docs and text files.
- [x] **Dataset records in the connector framework.** Batches carry full table snapshots that are diffed into `data.*`
      tables, with schema drift (`alter_dataset`) and deletions (`archive_dataset`) going through the harness. Why: split
      from "Drive Sheets as datasets"; datasets aren't items, so the framework needed a dataset record kind first.
- [x] **Drive Sheets as datasets.** In `gdrive`, export each Sheet tab as CSV into a dataset record (key
      `<file id>#<sheet id>`), with removed tabs/files as `deleted_datasets`. Why: the connector half of the split item.
- [x] **`ask` audit log.** Optional log of questions asked (who asked what, which items were searched and cited).
      Why: v1 `ask` left no trace of what was read. Split from "`ask` follow-ups".
- [x] **`ask` multi-turn context.** Follow-up questions that build on earlier ones. Why: the other half of the split
      "`ask` follow-ups" item; v1 `ask` is stateless.
- [x] **Per-team notification routing.** Send a team's pending decisions to that team's channel (webhook URLs are
      secrets, so map team → env name, not URL, in the DB). Why: v1 sends every decision to every webhook.
- [x] "As of" queries: read any item or search results as they were at a given time.
- [x] **`ask` as of a time.** Answer from items as they read at `as_of` (full-text passages from the old version).
      Why: split from "As of" queries; v1 time travel covers `get_item` and `search` only.

- [x] **Notices for `auto_notice` changes.** Send stale marks and merges that applied automatically to the same
      webhooks, with their revert deadline. Why: `auto_notice` promises "a human can revert within N days", but only
      pending decisions were ever announced, so nobody heard about the change in time to revert it.
- [x] **Readers on synced records.** Let a connector batch create items restricted to named readers (e.g. from a
      Drive file's permissions). Why: v1 sync batches can't set readers, so private sources land team-wide.
- [x] **Drive permissions as readers.** In `gdrive`, map each file's permissions (users, not "anyone"/domain links)
      to actor names and send them as `readers`. Needs an actor ↔ email mapping (actors only have a name today). Why:
      split from "Readers on synced records"; the framework now accepts readers but no built-in connector sends them.
- [x] **Lift a synced restriction when the source goes public.** A record needs an explicit "public" signal (e.g. a
      Drive file shared with anyone or the domain) that proposes lifting the item's ACL. Why: split from "Drive
      permissions as readers"; None never changes access, so a file opened up in Drive stays restricted in DB4AI.
- [x] **Notion connector** on the connector framework (token from env, pages as documents).
      Why: the next source named in SPEC §6.
- [x] **Notion databases as datasets.** Sync each database shared with the integration as a dataset (properties as
      columns, rows = its pages, values as text), with schema drift and deletions as for Sheets. Why: split from the
      Notion connector; v1 syncs only page bodies, so a database's table data isn't queryable.
- [x] **Slack connector** on the connector framework (bot token from env, channel messages as one `conversation` item
      per channel per day, private channels restricted to their members). Why: the next source named in SPEC §6
      once the roadmap was empty.
- [x] **Slack threads older than the window.** A reply to a parent older than the 7-day re-read window is never seen;
      use each message's `latest_reply` (or the Events API) to re-render old days with new thread activity. Why: found
      while building the Slack connector.

## Later

- [x] Per-item ACLs beyond team scope. (v1 narrows within a team: named readers + owners + admins.)
- [x] **Sharing an item with another team.** Grant read access to actors or teams outside the item's team. Why: split
      from per-item ACLs; v1 only narrows access, it never widens it.
- [x] Evaluation set for the maintainers (dedup, staleness precision) for use with the real Claude path.
- [x] **Heuristic staleness precision.** The offline `contradiction` flags two different projects' dates as a
      contradiction (`stale-other-subject` in `db4ai eval`); require the changed fact to share its subject. Raise the
      floors in `tests/test_evals.py`. Why: found by the eval set; `auto_notice` stale marks land without review.
