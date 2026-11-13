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
- **Self-maintenance.** Background maintainers enrich items (summary, tags, entities, embeddings), link related
  items, merge near-duplicates, mark memories stale when newer items contradict them, propose turning
  recurring metadata into real tables, and clean up after themselves.
- **Risk tiers, fail closed.** `auto` changes apply and are logged. `auto_notice` changes apply and can be
  reverted for N days. `ask_first` changes wait in the AIDBHarness. A per-team policy table, not the AI,
  decides the tier, and unknown actions are always `ask_first`.
- **AIDBHarness.** Approving a decision applies the change in the same transaction. Rejections are
  remembered, and expired decisions are never applied. You can answer from Claude (MCP), the web page or the HTTP API.
- **Built for Claude.** An MCP server with `save_meeting`, `save_document`, `remember`, `log_decision`, `search`,
  `get_item`, `history`, `revert`, `dataset`, `harness_list_pending`, `harness_decide` and `harness_revert`.
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

## Configuration

| env | default | |
|---|---|---|
| `DB4AI_DATABASE_URL` | `postgresql://localhost/db4ai` | |
| `DB4AI_BLOB_URL` | `file://./.db4ai-blobs` | or `s3://bucket/prefix` (+ `DB4AI_S3_ENDPOINT` for MinIO) |
| `DB4AI_LLM` | `auto` | `claude` / `heuristic` |
| `DB4AI_MODEL` | `claude-opus-5-5` | model the maintainers use |
| `DB4AI_EMBEDDER` | `hash` | `voyage` (needs `VOYAGE_API_KEY`) |
| `DB4AI_LINK_THRESHOLD` / `DB4AI_DUP_THRESHOLD` | `0.55` / `0.92` | similarity thresholds |

## Not in v1

Connectors for Google Drive, Zoom/Meet, Notion, Miro and Slack (they will build on `POST /items`),
Slack/Teams notifications for pending decisions, and bitemporal "as of" queries.
