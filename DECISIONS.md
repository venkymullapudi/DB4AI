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
