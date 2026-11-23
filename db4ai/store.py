"""Append-only item store (SPEC §3). Every write is a new version with provenance."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg import sql
from psycopg.types.json import Jsonb

from .auth import Actor
from .config import settings
from .db import Conn, Provenance, log_change, vector_enabled

ITEM_TYPES = ("meeting", "document", "memory", "decision", "whiteboard", "note")


def content_hash(title: str, body: str, blob_sha256: str | None) -> str:
    return hashlib.sha256(json.dumps([title, body, blob_sha256]).encode()).hexdigest()


def enqueue(conn: Conn, kind: str, payload: dict, dedupe_key: str | None = None, delay_seconds: float = 0) -> None:
    """Outbox: queued in the caller's transaction, so a job exists iff the write committed."""
    conn.execute(
        "insert into jobs (kind, payload, dedupe_key, run_after)"
        " values (%s, %s, %s, now() + make_interval(secs => %s))"
        " on conflict (dedupe_key) where status = 'queued' do nothing",
        (kind, Jsonb(payload), dedupe_key, delay_seconds),
    )


def _current(conn: Conn, item_id: UUID) -> dict | None:
    return conn.execute("select * from items_current where id = %s", (item_id,)).fetchone()


def _insert_version(conn: Conn, prov: Provenance, item_id: UUID, base: dict | None, *,
                    title: str, body: str, metadata: dict, status: str = "active",
                    merged_into: UUID | None = None, blob_sha256: str | None = None) -> dict:
    # Serialise writers of the same item (row lock; items itself is never updated).
    conn.execute("select id from items where id = %s for update", (item_id,))
    if base is not None and base["status"] == "erased":
        raise ValueError(f"item {item_id} was erased; it accepts no new versions")
    row = conn.execute("select coalesce((select version from item_heads where item_id = %s), 0) + 1 as v",
                       (item_id,)).fetchone()
    chash = content_hash(title, body, blob_sha256)
    ver = conn.execute(
        "insert into item_versions (item_id, version, title, body, metadata, status, merged_into, blob_sha256,"
        " content_hash, actor_id, source, reason, harness_decision_id)"
        " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning version, created_at",
        (item_id, row["v"], title, body, Jsonb(metadata), status, merged_into, blob_sha256, chash,
         prov.actor_id, prov.source, prov.reason, prov.decision_id),
    ).fetchone()
    log_change(conn, prov, "item", str(item_id), "version",
               before=_summary(base) if base else None,
               after={"version": ver["version"], "title": title, "status": status, "content_hash": chash})
    if base is None or base["content_hash"] != chash:
        enqueue(conn, "enrich", {"item_id": str(item_id)}, dedupe_key=f"enrich:{item_id}")
    return {"item_id": item_id, "version": ver["version"], "changed": True}


def _summary(cur: dict) -> dict:
    return {"version": cur["version"], "title": cur["title"], "status": cur["status"],
            "content_hash": cur["content_hash"]}


def save_item(conn: Conn, actor: Actor, prov: Provenance, *, team_id: UUID, type: str, title: str,
              body: str = "", metadata: dict | None = None, external_key: str | None = None,
              blob_sha256: str | None = None) -> dict:
    """Create an item, or add a version if `external_key` already exists and content changed.

    Idempotent: re-saving identical content adds no version.
    """
    actor.require_write(team_id)
    metadata = metadata or {}
    item_id = None
    if external_key is not None:
        row = conn.execute("select id from items where team_id=%s and type=%s and external_key=%s",
                           (team_id, type, external_key)).fetchone()
        item_id = row["id"] if row else None
        if item_id is None:  # an erased item keeps a hash of its key so a re-import can't resurrect it
            from .erasure import withheld_key
            gone = conn.execute("select id from items where team_id=%s and type=%s and external_key=%s",
                                (team_id, type, withheld_key(external_key))).fetchone()
            if gone is not None:
                return {"item_id": gone["id"], "version": None, "changed": False, "erased": True}
    if item_id is None:
        item_id = conn.execute(
            "insert into items (team_id, type, external_key, created_by) values (%s,%s,%s,%s)"
            " on conflict (team_id, type, external_key) do nothing returning id",
            (team_id, type, external_key, actor.id),
        ).fetchone()
        if item_id is None:  # lost a race with a concurrent importer
            item_id = conn.execute("select id from items where team_id=%s and type=%s and external_key=%s",
                                   (team_id, type, external_key)).fetchone()
        item_id = item_id["id"]
        log_change(conn, prov, "item", str(item_id), "create", after={"type": type, "team_id": str(team_id)})
    base = _current(conn, item_id)
    if base is not None and base["status"] == "erased":
        return {"item_id": item_id, "version": base["version"], "changed": False, "erased": True}
    if base is not None:
        merged_meta = {**base["metadata"], **metadata}
        if (base["content_hash"] == content_hash(title, body, blob_sha256)
                and merged_meta == base["metadata"]):
            return {"item_id": item_id, "version": base["version"], "changed": False}
        metadata = merged_meta
    # Keep lifecycle state: re-importing a file must not revive an item a human archived or merged.
    return _insert_version(conn, prov, item_id, base, title=title, body=body, metadata=metadata,
                           blob_sha256=blob_sha256,
                           status=base["status"] if base else "active",
                           merged_into=base["merged_into"] if base else None)


def new_version(conn: Conn, prov: Provenance, item_id: UUID, *, title: str | None = None,
                body: str | None = None, metadata_patch: dict | None = None, status: str | None = None,
                merged_into: UUID | None = None) -> dict:
    """Derive a new version from the current one (used by maintainers and harness actions)."""
    cur = _current(conn, item_id)
    if cur is None:
        raise KeyError(f"item {item_id} not found")
    meta = dict(cur["metadata"])
    for k, v in (metadata_patch or {}).items():
        if v is None:
            meta.pop(k, None)
        else:
            meta[k] = v
    return _insert_version(
        conn, prov, item_id, cur,
        title=cur["title"] if title is None else title,
        body=cur["body"] if body is None else body,
        metadata=meta,
        status=cur["status"] if status is None else status,
        merged_into=merged_into if merged_into is not None else cur["merged_into"],
        blob_sha256=cur["blob_sha256"],
    )


def get_item(conn: Conn, actor: Actor, item_id: UUID) -> dict:
    cur = _current(conn, item_id)
    if cur is None:
        raise KeyError(f"item {item_id} not found")
    actor.require_read(cur["team_id"])
    cur["links"] = conn.execute(
        "select l.kind, l.confidence, l.reason, case when l.src_item = %(id)s then l.dst_item else l.src_item end as other,"
        " c.title as other_title, c.type as other_type"
        " from links l join items_current c on c.id = (case when l.src_item = %(id)s then l.dst_item else l.src_item end)"
        " where l.src_item = %(id)s or l.dst_item = %(id)s order by l.confidence desc nulls last",
        {"id": item_id},
    ).fetchall()
    return cur


def history(conn: Conn, actor: Actor, item_id: UUID) -> list[dict]:
    team = conn.execute("select team_id from items where id = %s", (item_id,)).fetchone()
    if team is None:
        raise KeyError(f"item {item_id} not found")
    actor.require_read(team["team_id"])
    return conn.execute(
        "select v.version, v.title, v.status, v.source, v.reason, v.harness_decision_id, v.created_at,"
        " a.name as actor, a.kind as actor_kind"
        " from item_versions v join actors a on a.id = v.actor_id where v.item_id = %s order by v.version",
        (item_id,),
    ).fetchall()


def revert(conn: Conn, actor: Actor, prov: Provenance, item_id: UUID, to_version: int) -> dict:
    """Revert = a new version copying an earlier one. Nothing is lost."""
    team = conn.execute("select team_id from items where id = %s", (item_id,)).fetchone()
    if team is None:
        raise KeyError(f"item {item_id} not found")
    actor.require_write(team["team_id"])
    old = conn.execute("select * from item_versions where item_id=%s and version=%s", (item_id, to_version)).fetchone()
    if old is None:
        raise KeyError(f"item {item_id} has no version {to_version}")
    cur = _current(conn, item_id)
    return _insert_version(conn, prov, item_id, cur, title=old["title"], body=old["body"],
                           metadata=old["metadata"], status=old["status"], merged_into=old["merged_into"],
                           blob_sha256=old["blob_sha256"])


def add_link(conn: Conn, prov: Provenance, src: UUID, dst: UUID, kind: str = "related",
             confidence: float | None = None) -> bool:
    if src == dst:
        return False
    a, b = (src, dst) if kind in ("mentions", "wikilink", "duplicate_of", "supersedes") else sorted([src, dst], key=str)
    row = conn.execute(
        "insert into links (src_item, dst_item, kind, confidence, actor_id, reason, harness_decision_id)"
        " values (%s,%s,%s,%s,%s,%s,%s) on conflict do nothing returning id",
        (a, b, kind, confidence, prov.actor_id, prov.reason, prov.decision_id),
    ).fetchone()
    if row:
        log_change(conn, prov, "link", str(row["id"]), "insert",
                   after={"src": str(a), "dst": str(b), "kind": kind, "confidence": confidence})
    return row is not None


# ---------------------------------------------------------------- embeddings & search

MAX_CANDIDATES = 1000  # pgvector's hnsw.ef_search ceiling


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def register_model(conn: Conn, model: str, dims: int) -> None:
    """Record `model`'s fixed dimension count (first use) or check it. Creates its HNSW indexes on first use."""
    if dims <= 0:
        raise ValueError(f"embedding for {model!r} is empty")
    new = conn.execute("insert into embedding_models (model, dims) values (%s, %s) on conflict do nothing"
                       " returning dims", (model, dims)).fetchone()
    if new is not None:
        conn.execute("select db4ai_ensure_vector_index(%s, %s)", (model, dims))
        return
    have = conn.execute("select dims from embedding_models where model = %s", (model,)).fetchone()["dims"]
    if have != dims:
        raise ValueError(f"embedding model {model!r} has {have} dimensions, got {dims}")


def model_dims(conn: Conn, model: str) -> int | None:
    row = conn.execute("select dims from embedding_models where model = %s", (model,)).fetchone()
    return row["dims"] if row else None


def _distance(conn: Conn, col: str, dims: int) -> tuple[str, str]:
    """(SQL cosine-distance expression against one query-vector parameter, cast for the stored value).

    With pgvector the expression matches the per-model partial HNSW index (`embedding::vector(dims)`).
    """
    if vector_enabled(conn):
        return f"({col}::vector({dims}) <=> %(q)s::vector({dims}))", f"%s::vector({dims})"
    return f"(1 - db4ai_cosine({col}, %(q)s::float8[]))", "%s::float8[]"


def _store_value(conn: Conn, vec: list[float]) -> Any:
    return _vec_literal(vec) if vector_enabled(conn) else vec


def put_embedding(conn: Conn, item_id: UUID, model: str, version: int, vec: list[float]) -> None:
    register_model(conn, model, len(vec))
    _, cast = _distance(conn, "embedding", len(vec))
    conn.execute(
        f"insert into item_embeddings (item_id, model, version, embedding) values (%s, %s, %s, {cast})"
        " on conflict (item_id, model) do update set version = excluded.version, embedding = excluded.embedding",
        (item_id, model, version, _store_value(conn, vec)),
    )


def put_chunks(conn: Conn, item_id: UUID, model: str, version: int, chash: str,
               spans: list[tuple[int, int]], vecs: list[list[float]]) -> None:
    """Replace an item's chunks for `model` (derived data, so replacing is allowed)."""
    conn.execute("delete from item_chunks where item_id = %s and model = %s", (item_id, model))
    for no, ((start, end), vec) in enumerate(zip(spans, vecs)):
        register_model(conn, model, len(vec))
        _, cast = _distance(conn, "embedding", len(vec))
        conn.execute(
            "insert into item_chunks (item_id, model, chunk_no, version, content_hash, start_char, end_char, embedding)"
            f" values (%s, %s, %s, %s, %s, %s, %s, {cast})",
            (item_id, model, no, version, chash, start, end, _store_value(conn, vec)),
        )


def _nearest(conn: Conn, table: str, cols: str, finish: str, vec: list[float], model: str,
             params: dict, limit: int) -> list[dict]:
    """Nearest rows of `table` for `model`, filtered and ranked by `finish` (SQL over the `cand` CTE).

    First pass: the `candidates` nearest rows by an index-friendly `order by distance limit` (HNSW with
    pgvector), then the filters. If that leaves fewer than `limit` results and the pool was full, filters may
    have hidden closer matches, so it falls back to an exact scan. Results are the same either way; only speed
    differs.
    """
    dims = model_dims(conn, model)
    if dims is None:
        return []
    if len(vec) != dims:
        raise ValueError(f"embedding model {model!r} has {dims} dimensions, query has {len(vec)}")
    dist, _ = _distance(conn, "embedding", dims)
    pool = min(max(limit, settings().vector_candidates), MAX_CANDIDATES)
    p = {**params, "q": _store_value(conn, vec), "limit": limit}

    # The model is inlined as a literal so the partial index's `where model = ...` matches even under a
    # generic (prepared) plan.
    lit = sql.Literal(model).as_string(conn)

    def run(candidates: int | None) -> tuple[list[dict], int]:
        cap = "" if candidates is None else f" limit {candidates}"
        query = (f"with cand as materialized (select {cols}, {dist} as dist from {table}"
                 f" where model = {lit} order by dist{cap}),"
                 " pool as (select count(*) as _pool from cand) "
                 f"select r.*, pool._pool from pool left join ({finish}) r on true")
        with conn.transaction():
            if candidates is not None and vector_enabled(conn):
                conn.execute("select set_config('hnsw.ef_search', %s, true)", (str(max(candidates, 40)),))
            rows = conn.execute(query, p).fetchall()
        n = rows[0]["_pool"]
        return [{k: v for k, v in r.items() if k != "_pool"} for r in rows if r["id"] is not None], n

    rows, n = run(pool)
    if len(rows) < limit and n >= pool:
        rows, _ = run(None)
    return rows


def similar_chunks(conn: Conn, vec: list[float], model: str, *, team_ids: list[UUID] | None,
                   limit: int = 10, types: list[str] | None = None) -> list[dict]:
    """Best-matching chunk per item, for chunks cut from the item's current content."""
    finish = (
        "select * from ("
        " select distinct on (c.id) c.id, c.version, c.title, c.type, c.team_id, c.status, c.updated_at, 1 - k.dist as score,"
        " k.chunk_no, k.start_char, k.end_char,"
        " substr(c.body, k.start_char + 1, k.end_char - k.start_char) as chunk_text"
        " from cand k join items_current c on c.id = k.item_id and c.content_hash = k.content_hash"
        " where c.status = 'active'"
        " and (%(teams)s::uuid[] is null or c.team_id = any(%(teams)s::uuid[]))"
        " and (%(types)s::text[] is null or c.type = any(%(types)s::text[]))"
        " order by c.id, k.dist) best order by score desc limit %(limit)s"
    )
    return _nearest(conn, "item_chunks", "item_id, content_hash, chunk_no, start_char, end_char", finish,
                    vec, model, {"teams": team_ids, "types": types}, limit)


def similar(conn: Conn, vec: list[float], model: str, *, team_ids: list[UUID] | None,
            exclude: UUID | None = None, limit: int = 10, types: list[str] | None = None) -> list[dict]:
    finish = (
        "select c.id, c.title, c.type, c.team_id, c.status, c.updated_at, 1 - e.dist as score"
        " from cand e join items_current c on c.id = e.item_id"
        " where c.status = 'active'"
        " and (%(teams)s::uuid[] is null or c.team_id = any(%(teams)s::uuid[]))"
        " and (%(exclude)s::uuid is null or c.id <> %(exclude)s::uuid)"
        " and (%(types)s::text[] is null or c.type = any(%(types)s::text[]))"
        " order by e.dist limit %(limit)s"
    )
    return _nearest(conn, "item_embeddings", "item_id", finish, vec, model,
                    {"teams": team_ids, "types": types, "exclude": exclude}, limit)


def search(conn: Conn, actor: Actor, query: str, *, embedder=None, limit: int = 10,
           types: list[str] | None = None, include_inactive: bool = False) -> list[dict]:
    """Hybrid search: full-text rank + chunk embedding similarity, restricted to the actor's teams.

    Each result carries `chunk` (no, start, end, text): the item's best-matching chunk by vector score, or
    None if the item was found only by full-text search.
    """
    teams = None if actor.is_admin else list(actor.teams)
    fts = conn.execute(
        "select c.id, c.version, c.title, c.type, c.team_id, c.status, c.updated_at,"
        " ts_rank(to_tsvector('english', c.title || ' ' || c.body), plainto_tsquery('english', %s)) as score"
        " from items_current c"
        " where to_tsvector('english', c.title || ' ' || c.body) @@ plainto_tsquery('english', %s)"
        " and (%s or c.status = 'active')"
        " and (%s::uuid[] is null or c.team_id = any(%s::uuid[]))"
        " and (%s::text[] is null or c.type = any(%s::text[]))"
        " order by score desc limit %s",
        (query, query, include_inactive, teams, teams, types, types, limit * 2),
    ).fetchall()
    results: dict[UUID, dict] = {}
    for r in fts:
        results[r["id"]] = {**r, "fts": float(r["score"]), "vec": 0.0, "chunk": None}
    if embedder is not None:
        vec = embedder.embed([query])[0]
        for r in similar_chunks(conn, vec, embedder.name, team_ids=teams, limit=limit * 2, types=types):
            chunk = {"no": r.pop("chunk_no"), "start": r.pop("start_char"), "end": r.pop("end_char"),
                     "text": r.pop("chunk_text")}
            entry = results.setdefault(r["id"], {**r, "fts": 0.0, "vec": 0.0})
            entry["vec"], entry["chunk"] = float(r["score"]), chunk
    ranked = sorted(results.values(), key=lambda r: min(r["fts"], 1.0) * 0.5 + r["vec"], reverse=True)
    for r in ranked:
        r["score"] = round(min(r["fts"], 1.0) * 0.5 + r["vec"], 4)
        r.pop("fts"), r.pop("vec")
    return ranked[:limit]


# ---------------------------------------------------------------- "as of" reads

def _version_as_of(conn: Conn, item_id: UUID, as_of: datetime) -> dict | None:
    """The item as it read at `as_of`: its latest version created at or before then (same shape as items_current)."""
    return conn.execute(
        "select i.id, i.team_id, i.type, i.external_key, i.created_at, i.created_by,"
        " v.version, v.title, v.body, v.metadata, v.status, v.merged_into, v.blob_sha256,"
        " v.content_hash, v.actor_id as updated_by, v.source, v.reason, v.created_at as updated_at"
        " from items i join item_versions v on v.item_id = i.id"
        " where i.id = %s and v.created_at <= %s order by v.version desc limit 1",
        (item_id, as_of),
    ).fetchone()


def _links_as_of(conn: Conn, item_id: UUID, as_of: datetime) -> list[dict]:
    """Links of the item that existed at `as_of`, rebuilt from the append-only change_log (links can be deleted)."""
    rows = conn.execute(
        "select ins.after->>'kind' as kind, (ins.after->>'confidence')::real as confidence,"
        " case when ins.after->>'src' = %(id)s then ins.after->>'dst' else ins.after->>'src' end as other"
        " from change_log ins"
        " where ins.entity = 'link' and ins.op = 'insert' and ins.at <= %(t)s"
        " and (ins.after->>'src' = %(id)s or ins.after->>'dst' = %(id)s)"
        " and not exists (select 1 from change_log del where del.entity = 'link' and del.op = 'delete'"
        "  and del.entity_id = ins.entity_id and del.at <= %(t)s)"
        " order by confidence desc nulls last",
        {"id": str(item_id), "t": as_of},
    ).fetchall()
    out = []
    for r in rows:
        other = _version_as_of(conn, UUID(r["other"]), as_of)
        if other is not None:
            out.append({"kind": r["kind"], "confidence": r["confidence"], "other": other["id"],
                        "other_title": other["title"], "other_type": other["type"]})
    return out


def get_item_as_of(conn: Conn, actor: Actor, item_id: UUID, as_of: datetime) -> dict:
    """The item as it was at `as_of`, plus `as_of`, `current_version` and `current_status`.

    Unknown items and items created after `as_of` are both "not found". An erased item's old versions were
    scrubbed, so they read back as the tombstone text; `current_status` says so.
    """
    team = conn.execute("select team_id from items where id = %s", (item_id,)).fetchone()
    if team is None:
        raise KeyError(f"item {item_id} not found")
    actor.require_read(team["team_id"])
    row = _version_as_of(conn, item_id, as_of)
    if row is None:
        raise KeyError(f"item {item_id} did not exist at {as_of.isoformat()}")
    cur = _current(conn, item_id)
    row["as_of"], row["current_version"], row["current_status"] = as_of, cur["version"], cur["status"]
    row["links"] = _links_as_of(conn, item_id, as_of)
    return row


def search_as_of(conn: Conn, actor: Actor, query: str, as_of: datetime, *, limit: int = 10,
                 types: list[str] | None = None, include_inactive: bool = False) -> list[dict]:
    """Full-text search over items as they read at `as_of` (each item's latest version at or before then).

    Vector similarity is skipped: chunks and embeddings are derived from current content only, so an old
    version has none. Each result has `chunk` None and the matching `version`. Items erased since are left out.
    """
    teams = None if actor.is_admin else list(actor.teams)
    rows = conn.execute(
        "with v as (select distinct on (v.item_id) v.item_id, v.version, v.title, v.body, v.status, v.created_at"
        "  from item_versions v where v.created_at <= %(t)s order by v.item_id, v.version desc)"
        " select i.id, v.version, v.title, i.type, i.team_id, v.status, v.created_at as updated_at,"
        " ts_rank(to_tsvector('english', v.title || ' ' || v.body), plainto_tsquery('english', %(q)s)) as score"
        " from v join items i on i.id = v.item_id join items_current c on c.id = i.id"
        " where to_tsvector('english', v.title || ' ' || v.body) @@ plainto_tsquery('english', %(q)s)"
        " and c.status <> 'erased' and (%(inactive)s or v.status = 'active')"
        " and (%(teams)s::uuid[] is null or i.team_id = any(%(teams)s::uuid[]))"
        " and (%(types)s::text[] is null or i.type = any(%(types)s::text[]))"
        " order by score desc, i.id limit %(limit)s",
        {"t": as_of, "q": query, "inactive": include_inactive, "teams": teams, "types": types, "limit": limit},
    ).fetchall()
    for r in rows:
        r["score"] = round(min(float(r["score"]), 1.0) * 0.5, 4)
        r["chunk"] = None
    return rows
