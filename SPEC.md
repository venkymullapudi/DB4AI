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
- Embeddings use **pgvector** when the extension is available, otherwise a `float8[]` column
  with a SQL cosine function (same API).

## 3. Data model — append-only with provenance
- Every unstructured thing is an `item` (stable id, team, type) with a chain of `item_versions`.
  Versions are **never updated or deleted** (enforced by trigger). Current state = latest version.
- Each version records `actor`, `source`, `reason`, and `harness_decision_id` if a human approved it.
- Status (active / archived / merged) is part of the version, so archiving and merging are also just new versions.
- Revert = new version that copies an earlier one.
- Structured datasets are real tables in the `data` schema. A row-level trigger writes every
  change to the shared append-only `change_log` with the same actor/source/reason fields.

## 4. Self-maintenance — six maintainer jobs
1. **enrich** — summary, tags, entities, embedding for new/changed content.
2. **link** — connect related items (embedding similarity + shared entities, Obsidian wikilinks).
3. **dedup** — near-duplicate items → propose merge.
4. **staleness** — newer items contradicting older memories → propose mark-stale.
5. **schema** — recurring metadata shape across items of one type → propose promoting it to a table.
6. **hygiene** — orphan blob GC, embedding rebuilds, expiring harness decisions.

### Risk tiers (policy-driven, fail closed)
| tier | behaviour | default action types |
|---|---|---|
| `auto` | apply now, logged | `add_tags`, `add_link`, `set_summary`, `set_entities` |
| `auto_notice` | apply now; a human can revert within N days | `mark_stale`, `merge_items` (confidence ≥ threshold) |
| `ask_first` | queued in the harness; nothing happens until approved | `archive_item`, `promote_schema`, `merge_items` (low confidence), `resolve_contradiction`, `change_access`, **any unknown action** |

Tiers come from the `policies` table (per team, with a global default). The AI never picks a tier.

## 5. AIDBHarness
- `harness_decisions` table: action, payload, diff, evidence, confidence, tier, scope team,
  status, expiry, decider, note, fingerprint.
- **Approve applies the change in the same transaction** that records the decision.
  Expired decisions are **not** applied. Rejections are remembered (fingerprint), so the same proposal isn't made again.
- Surfaces: MCP tools (`harness_list_pending`, `harness_decide`), a minimal web page (`/harness`),
  and the HTTP API. Slack/Teams notifications are v1.1.

## 6. Ingestion (push-based)
- MCP write tools: `save_meeting`, `save_document`, `remember`, `log_decision`, `search`, `get_item`, `history`, `revert`.
- CLI import of files/folders: md, txt, pdf, docx → items; csv → structured dataset.
- Memory repo import: Markdown/Obsidian vault (frontmatter → metadata, `[[wikilinks]]` → links);
  re-import only adds versions for changed files.
- `POST /items` HTTP endpoint.
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
