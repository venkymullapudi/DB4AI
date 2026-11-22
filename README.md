# DB4AI
Self Evolving and Self Maintaining DB for AI. Especially for orgs using Claude(Meetings, Whiteboards, sheets, Data and so on). Make the Human+AI made data with ACID principles

One self-hosted Postgres instance per company holds **meetings, documents, team memory, decisions and
analytics tables** side by side, keeps itself tidy with AI maintainers, and asks a human through the
**AIDBHarness** whenever a change is risky. The full design is in [SPEC.md](SPEC.md).

## What you get in v1

- **ACID across everything.** Postgres is the only system of record. Items, versions, embeddings, links,
  analytics tables, harness decisions and the job queue all commit in one transaction. Blobs are written
  under their content hash *before* the row that references them commits, so a row never points at a missing blob.
- **Nothing is overwritten.** Every edit is a new version that records who made it (a human or an agent),
  where it came from and why. Database triggers block `UPDATE`/`DELETE` on history. Undo is a new version.
- **Erasure and redaction.** The one exception to append-only: an admin-approved harness decision can erase an
  item's content from every version, blob, embedding, audit row and related decision (`POST /items/{id}/erase`), or
  replace chosen terms with `[redacted]` everywhere (`POST /items/{id}/redact`). Anyone who can read the item can
  ask; only an admin can approve. Ids, version numbers and provenance stay, and a tombstone
  (`GET /items/{id}/erasures`) records what kind of erasure it was, who asked, who approved and why.
  Copies go too: `data.*` rows keyed by the item (and their change log) are scrubbed in the same transaction, and
  each other item that quotes the erased text or holds it as a merge alias gets its own `redact_item` proposal.
- **Self-maintenance.** Background maintainers enrich items (summary, tags, entities, embeddings), link related
  items, merge near-duplicates, mark memories stale when newer items contradict them, propose turning
  recurring metadata into real tables, and clean up after themselves.
- **Risk tiers, fail closed.** `auto` changes apply and are logged. `auto_notice` changes apply and can be
  reverted for N days. `ask_first` changes wait in the AIDBHarness. A per-team policy table, not the AI,
  decides the tier, and unknown actions are always `ask_first`.
- **AIDBHarness.** Approving a decision applies the change in the same transaction. Rejections are
  remembered, and expired decisions are never applied. You can answer from Claude (MCP), the web page or the HTTP API.
- **Answers with citations.** `ask` (MCP tool or `POST /ask`) answers a question from the items you can read and
  cites the item ids, versions and exact quotes it used; if nothing it can quote supports an answer, it says so.
- **Built for Claude.** An MCP server with `ask`, `save_meeting`, `save_document`, `remember`, `log_decision`, `search`,
  `get_item`, `history`, `revert`, `dataset`, `request_erasure`, `request_redaction`, `harness_list_pending`,
  `harness_decide` and `harness_revert`.
- **Ingestion.** A CLI imports Markdown/Obsidian vaults (frontmatter becomes metadata, `[[wikilinks]]` become links),
  md/txt/pdf/docx files, and CSV files (which become BI tables in the `data` schema). There is also `POST /items`.
- **BI-ready.** Structured data lives in real tables (`data.<team>_<name>`), so Tableau, Metabase and others connect directly.
  A trigger records every row change in the append-only `change_log`.

## Quick start (Docker)

```bash
docker compose up -d
docker compose exec api db4ai user you --team eng:owner --admin   # prints your API token
```

Open http://localhost:8000/harness and sign in with the token.

Import your team memory repo and some files:

```bash
docker compose cp ./memory-repo api:/tmp/vault
docker compose exec api db4ai import /tmp/vault --team eng --as you --vault
```

The `worker` service runs the maintainers continuously. Set `ANTHROPIC_API_KEY` to use Claude
(`DB4AI_MODEL`, default `claude-opus-5-5`). Without it, an offline heuristic maintainer is used.

## Use it from Claude (MCP)

```json
{
  "mcpServers": {
    "db4ai": {
      "command": "db4ai",
      "args": ["mcp"],
      "env": { "DB4AI_DATABASE_URL": "postgresql://db4ai:db4ai@localhost/db4ai", "DB4AI_TOKEN": "db4ai_..." }
    }
  }
}
```

Every write Claude makes is attributed to the token's actor, with source `claude-mcp`.

## Local development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

The tests start a throwaway Postgres cluster with your local `initdb`/`pg_ctl`, or use
`DB4AI_TEST_DATABASE_URL`. pgvector is used when installed. Otherwise embeddings fall back to `float8[]`
with a SQL cosine function.

CI (`.github/workflows/ci.yml`) runs the suite against `pgvector/pgvector:pg16` (Python 3.11 and 3.12) and
against plain `postgres:16` for the `float8[]` fallback. `DB4AI_EXPECT_PGVECTOR=1|0` makes a test fail if the
expected embedding backend isn't the one in use. It also runs `scripts/smoke.sh`, which builds the Docker
deployment, writes an item over HTTP, runs the maintainers and searches for it. Run it yourself with
`scripts/smoke.sh` (needs Docker; it removes its containers and volumes afterwards).

## Configuration

| env | default | |
|---|---|---|
| `DB4AI_DATABASE_URL` | `postgresql://localhost/db4ai` | |
| `DB4AI_BLOB_URL` | `file://./.db4ai-blobs` | or `s3://bucket/prefix` (+ `DB4AI_S3_ENDPOINT` for MinIO) |
| `DB4AI_LLM` | `auto` | `claude` / `heuristic` |
| `DB4AI_MODEL` | `claude-opus-5-5` | model the maintainers use |
| `DB4AI_EMBEDDER` | `hash` | `voyage` (needs `VOYAGE_API_KEY`) |
| `DB4AI_CHUNK_CHARS` | `1500` | long items are split into chunks of at most this size; search returns the best chunk |
| `DB4AI_VECTOR_CANDIDATES` | `200` | nearest rows taken from the vector index before team/type filters; too few results after filtering falls back to an exact scan |
| `DB4AI_LINK_THRESHOLD` / `DB4AI_DUP_THRESHOLD` | `0.55` / `0.92` | similarity thresholds |

## Not in v1

Connectors for Google Drive, Zoom/Meet, Notion, Miro and Slack (they will build on `POST /items`),
Slack/Teams notifications for pending decisions, and bitemporal "as of" queries.
