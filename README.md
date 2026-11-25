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
- **Time travel.** `GET /items/{id}?as_of=<ISO time>` returns the item and its links as they were then;
  `GET /search?q=...&as_of=<ISO time>` searches items as they read then (full-text only). MCP `get_item`/`search`
  take the same `as_of`, and so does `ask` (`POST /ask` body field), which then answers and cites the old versions.
- **Erasure and redaction.** The one exception to append-only: an admin-approved harness decision can erase an
  item's content from every version, blob, embedding, audit row and related decision (`POST /items/{id}/erase`), or
  replace chosen terms with `[redacted]` everywhere (`POST /items/{id}/redact`). Anyone who can read the item can
  ask; only an admin can approve. Ids, version numbers and provenance stay, and a tombstone
  (`GET /items/{id}/erasures`) records what kind of erasure it was, who asked, who approved and why.
  Copies go too: `data.*` rows keyed by the item (and their change log) are scrubbed in the same transaction, and
  each other item that quotes the erased text or holds it as a merge alias gets its own `redact_item` proposal.
- **Per-item access.** Within a team, an item can be restricted to named readers: pass `readers` (actor names) to
  `POST /items` or MCP `save_document` when creating it, or ask to change it with `POST /items/{id}/access`
  (`{"readers": [...] | null, "reason": ...}`; always reviewed by a team owner or admin). Owners and admins always
  read; everyone else in the team gets "not found" from reads, search, `ask`, links and harness listings.
  `GET /items/{id}/access` shows who can read it.
- **Sharing across teams.** Ask to share an item, read-only, with other teams or outside actors with
  `POST /items/{id}/shares` (`{"teams": [...], "actors": [...], "reason": ...}`; the full new list, empty lists
  unshare; always reviewed by a team owner or admin). Sharees can read, search and `ask` over it; writes stay with
  the item's team.
- **Self-maintenance.** Background maintainers enrich items (summary, tags, entities, embeddings), link related
  items, merge near-duplicates, mark memories stale when newer items contradict them, propose turning
  recurring metadata into real tables, and clean up after themselves.
- **Risk tiers, fail closed.** `auto` changes apply and are logged. `auto_notice` changes apply, are announced to
  the notification webhooks, and can be reverted for N days. `ask_first` changes wait in the AIDBHarness. A per-team policy table, not the AI,
  decides the tier, and unknown actions are always `ask_first`.
- **AIDBHarness.** Approving a decision applies the change in the same transaction. Rejections are
  remembered, and expired decisions are never applied. You can answer from Claude (MCP), the web page or the HTTP API.
- **Answers with citations.** `ask` (MCP tool or `POST /ask`) answers a question from the items you can read and
  cites the item ids, versions and exact quotes it used; if nothing it can quote supports an answer, it says so.
  Follow-ups pass the earlier turns as `history`, so "and who owns it?" knows what "it" is; only items are cited.
- **Built for Claude.** An MCP server with `ask`, `save_meeting`, `save_document`, `remember`, `log_decision`, `search`,
  `get_item`, `history`, `revert`, `dataset`, `request_erasure`, `request_redaction`, `harness_list_pending`,
  `harness_decide` and `harness_revert`.
- **Ingestion.** A CLI imports Markdown/Obsidian vaults (frontmatter becomes metadata, `[[wikilinks]]` become links),
  md/txt/pdf/docx files, and CSV files (which become BI tables in the `data` schema). There is also `POST /items`.
- **Connectors with incremental sync.** `db4ai sync transcripts <folder> --team eng` turns meeting transcripts
  (`.vtt`, `.srt`, `.txt`, e.g. a Zoom/Meet/Teams export folder) into `meeting` items with speakers and duration,
  and only reads files newer than its cursor on the next run. Connectors that run elsewhere push batches to
  `POST /sync/{source}` (`items`, `deleted`, `cursor`, `expected_cursor`) and read `GET /sync/{source}/cursor`: the items and the
  cursor commit together, and a stale `expected_cursor` is a 409, so no batch is skipped or applied twice.
  An item can carry `readers` (actor names who can read it in the source): a new item is created restricted to them
  (plus the syncer); names outside the team are dropped. When the source's readers change later, the sync proposes
  `set_item_access` for an owner to approve. `public: true` says everybody can read it in the source, which proposes
  lifting an existing restriction.
  Sources that only know emails send `reader_emails`, matched to actors' emails (`db4ai user NAME --email ...` or
  `db4ai email NAME ADDRESS`).
  `db4ai sync gdrive --team eng` syncs Google Docs (exported as text), text/Markdown files, and PDFs and .docx files (text extracted) from Google Drive
  (including shared drives), and each Google Sheets tab as a dataset table `data.<team>_<name>` (diffed on every change;
  column changes and removed tabs wait for approval in the harness), through the Drive changes API, with an OAuth token you supply in the environment.
  `--folder <id>` (repeatable, or `DB4AI_GDRIVE_FOLDERS`) limits it to those folders or shared drives and everything
  below them; files that leave the scope count as deleted.
  `--readers` (or `DB4AI_GDRIVE_READERS=1`) restricts each synced file to the team members it is shared with in Drive
  (by email; files shared with anyone or your whole domain stay team-wide, groups are not expanded). When a restricted
  file is later shared with anyone or the domain, the sync proposes lifting the restriction for an owner to approve.
  `db4ai sync notion --team eng` syncs every Notion page shared with your integration (token in
  `DB4AI_NOTION_TOKEN`) as a document, re-reading only pages edited since the last sync; pages are team-wide, so share
  only what the team should see.
  A file deleted (or trashed) in the source proposes archiving its item in the harness; nothing is archived until a
  team owner approves, and the proposal is withdrawn if the file comes back first.
  A batch can also carry `datasets` (`key`, `name`, `columns`, `rows`: a full snapshot of one table, e.g. a sheet tab)
  and `deleted_datasets`. A snapshot becomes a BI table (`data.<team>_<name>`) and later snapshots are diffed into it
  row by row; new, removed or retyped columns wait for an `alter_dataset` approval, and a deleted dataset proposes
  `archive_dataset` (the table stays readable, syncing stops).
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

### Evaluating the maintainers

`db4ai eval` scores the dedup and staleness maintainers on labelled pairs (precision, recall, F1 and every miss)
with the same rules and LLM call `relate` uses. It needs no database and writes nothing. It uses the configured LLM
and embedder, so with `ANTHROPIC_API_KEY` set it measures Claude (`--llm heuristic|claude` overrides; Claude calls bill
your key). The built-in set is synthetic; pass `--cases my-pairs.json` (same format as `db4ai/evals/maintainers.json`)
to score pairs from your own data. `--dup-threshold 0.9` tries a threshold, `--min-precision 0.9` exits 1 if a task
scores below it.

```bash
db4ai eval --llm claude --min-precision 0.9
```

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
| `DB4AI_NOTIFY_WEBHOOKS` | — | comma-separated Slack/Teams incoming-webhook URLs notified of pending harness decisions and of `auto_notice` changes (stale marks, merges) that can still be reverted; prefix `teams:` for a Teams Workflows webhook (Adaptive Card) |
| `DB4AI_ASK_LOG` | `off` | audit log of `ask` calls (`GET /ask/log`): `ids` records who asked, a hash of the question and which item versions were searched and cited; `text` also keeps the question |
| `DB4AI_WEBHOOK_<NAME>` | — | a team's own Slack/Teams webhook (same format as `DB4AI_NOTIFY_WEBHOOKS`); route a team to it with `db4ai notify-route add <team> DB4AI_WEBHOOK_<NAME> --as <admin or owner>` or `PUT /teams/{team}/notify-routes/DB4AI_WEBHOOK_<NAME>`; erasure/redaction requests only go to the global webhooks |
| `DB4AI_NOTIFY_DELAY_SECONDS` | `60` | wait before sending, so a burst of proposals becomes one message |
| `DB4AI_GDRIVE_TOKEN` | — | Google Drive OAuth access token (scope `drive.readonly`) for `db4ai sync gdrive` |
| `DB4AI_NOTION_TOKEN` | — | Notion internal integration token for `db4ai sync notion` (syncs the pages shared with the integration) |
| `DB4AI_GDRIVE_READERS` | off | `1` = restrict synced Drive files to the team members they are shared with (matched by actor email) |
| `DB4AI_GDRIVE_FOLDERS` | — | comma-separated Drive folder / shared drive ids to limit `db4ai sync gdrive` to (default: everything the token can read) |
| `DB4AI_GDRIVE_REFRESH_TOKEN`, `DB4AI_GDRIVE_CLIENT_ID`, `DB4AI_GDRIVE_CLIENT_SECRET` | — | instead of (or with) the access token: refresh it when it expires |
| `DB4AI_PUBLIC_URL` | — | base URL of the web UI, used for the review link in notifications |
| `DB4AI_LINK_THRESHOLD` / `DB4AI_DUP_THRESHOLD` | `0.55` / `0.92` | similarity thresholds |

## Not in v1

Connectors for Notion databases, Miro and Slack (they will build on `POST /sync/{source}`), and bitemporal
"as of" queries.
