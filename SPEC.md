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

## 4. Self-maintenance — six maintainer jobs
1. **enrich** — summary, tags, entities, embedding for new/changed content. Summary, tags and entities are
   applied by one `enrich` action, so each enrichment is one harness decision and one new version.
2. **link** — connect related items (embedding similarity + shared entities, Obsidian wikilinks).
3. **dedup** — near-duplicate items → propose merge.
4. **staleness** — newer items contradicting older memories → propose mark-stale.
5. **schema** — recurring metadata shape across items of one type → propose promoting it to a table.
6. **hygiene** — orphan blob GC, embedding rebuilds, expiring harness decisions.

### Risk tiers (policy-driven, fail closed)
| tier | behaviour | default action types |
|---|---|---|
| `auto` | apply now, logged | `enrich`, `add_tags`, `add_link`, `set_summary`, `set_entities` |
| `auto_notice` | apply now; a human can revert within N days | `mark_stale`, `merge_items` (confidence ≥ threshold) |
| `ask_first` | queued in the harness; nothing happens until approved | `archive_item`, `promote_schema`, `merge_items` (low confidence), `resolve_contradiction`, `change_access`, `erase_item` and `redact_item` (always; admin-only), **any unknown action** |

Tiers come from the `policies` table (per team, with a global default). The AI never picks a tier.

## 5. AIDBHarness
- `harness_decisions` table: action, payload, diff, evidence, confidence, tier, scope team,
  status, expiry, decider, note, fingerprint.
- **Approve applies the change in the same transaction** that records the decision.
  Expired decisions are **not** applied. Rejections are remembered (fingerprint), so the same proposal isn't made again.
- Surfaces: MCP tools (`harness_list_pending`, `harness_decide`), a minimal web page (`/harness`),
  and the HTTP API. Slack/Teams notifications are v1.1.

## 6. Ingestion (push-based)
- MCP write tools: `save_meeting`, `save_document`, `remember`, `log_decision`, `search`, `get_item`, `history`, `revert`,
  `request_erasure`, `request_redaction`.
- **`ask`** (MCP tool and `POST /ask`): answers a question from the items the actor can read. It retrieves with search
  (team access, active items only, best chunk per item; full-text-only hits use their best-matching chunk), gives the
  numbered passages to the LLM (heuristic: extractive sentences), and returns `answered`, `answer`, `citations`
  (item id, version, title, quote) and the `sources` searched. Citations to passages it wasn't given are dropped;
  a quote not found verbatim in its passage is kept as `null`. Without at least one verbatim quote the answer is not
  returned (`answered: false`, fail closed). Read-only: no versions, decisions or logs are written.
- CLI import of files/folders: md, txt, pdf, docx → items; csv → structured dataset.
- Memory repo import: Markdown/Obsidian vault (frontmatter → metadata, `[[wikilinks]]` → links);
  re-import only adds versions for changed files.
- `POST /items` HTTP endpoint; `POST /items/{id}/erase`, `POST /items/{id}/redact` and `GET /items/{id}/erasures`.
- External connectors (Drive, Zoom, Notion, Miro, Slack) are v1.1, built on `POST /items`.

## 7. Stack
Python ≥3.11, FastAPI, psycopg 3 with plain SQL and a small SQL migration runner, pgvector,
Postgres-table job queue (`FOR UPDATE SKIP LOCKED`), Claude (`claude-sonnet-5-5`) for
enrichment and proposals with a heuristic fallback when no API key is set, pluggable embeddings
(Voyage, or a local hashing embedder for offline use), local or S3 blobs, docker compose, pytest against real Postgres.

## 8. Access control
- Teams; actors are `human` or `agent`. Roles: `admin`, `owner`, `member` (per team), `agent`.
- Every item belongs to one team; reads and writes are limited to the actor's teams (admins see all).
- API tokens (stored hashed). Harness decisions are routed to owners of the scope team; admins can decide anything.
