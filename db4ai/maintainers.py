"""The six maintainer jobs (SPEC §4).

Each handler does its slow work (LLM calls, embeddings) *outside* a transaction and returns a
`commit(conn)` callback. The worker runs that callback and marks the job done in one
transaction, so a job's effects are applied exactly once.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from typing import Callable
from uuid import UUID

from . import harness, store
from .auth import Actor, maintainer_agent
from .blobs import BlobStore, gc_orphans
from .config import settings
from .db import Conn
from .embeddings import Embedder

log = logging.getLogger(__name__)
Commit = Callable[[Conn], dict]
FACT_TYPES = ("memory", "decision", "document", "meeting", "note")
_RESERVED_META = {"ai", "aliases", "stale", "path", "tags", "links", "source_path"}


class Context:
    def __init__(self, conn: Conn, llm, embedder: Embedder, blobs: BlobStore | None = None):
        self.conn, self.llm, self.embedder, self.blobs = conn, llm, embedder, blobs
        self.settings = settings()
        self._agent: Actor | None = None

    @property
    def agent(self) -> Actor:
        if self._agent is None:
            with self.conn.transaction():
                self._agent = maintainer_agent(self.conn)
        return self._agent


# ---------------------------------------------------------------- 1. enrich

def enrich(ctx: Context, payload: dict) -> Commit | None:
    item_id = UUID(payload["item_id"])
    cur = ctx.conn.execute("select * from items_current where id=%s", (item_id,)).fetchone()
    if cur is None or cur["status"] != "active":
        return None
    result = ctx.llm.enrich(cur["title"], cur["body"])
    vec = ctx.embedder.embed([f"{cur['title']}\n{cur['body']}"])[0]

    def commit(conn: Conn) -> dict:
        agent = ctx.agent
        latest = conn.execute("select max(version) v from item_versions where item_id=%s", (item_id,)).fetchone()["v"]
        why = f"enrichment of v{cur['version']} by {ctx.llm.name}"
        for action, value in (("set_summary", result.summary), ("add_tags", result.tags),
                              ("set_entities", result.entities)):
            if value:
                harness.propose(conn, agent, action, {"item_id": str(item_id), "value": value,
                                                      "for_version": cur["version"]}, reason=why, confidence=1.0)
        store.put_embedding(conn, item_id, ctx.embedder.name, latest, vec)
        store.enqueue(conn, "relate", {"item_id": str(item_id)}, dedupe_key=f"relate:{item_id}")
        return {"tags": result.tags, "entities": result.entities}

    return commit


# ---------------------------------------------------------------- 2-4. link, dedup, staleness

def relate(ctx: Context, payload: dict) -> Commit | None:
    item_id = UUID(payload["item_id"])
    cur = ctx.conn.execute("select * from items_current where id=%s", (item_id,)).fetchone()
    if cur is None or cur["status"] != "active":
        return None
    emb = ctx.conn.execute("select embedding::text as e from item_embeddings where item_id=%s and model=%s",
                           (item_id, ctx.embedder.name)).fetchone()
    if emb is None:
        return None
    vec = [float(x) for x in emb["e"].strip("[]{}").split(",")]
    s = ctx.settings
    neighbours = store.similar(ctx.conn, vec, ctx.embedder.name, team_ids=[cur["team_id"]], exclude=item_id, limit=8)

    proposals: list[tuple[str, dict, str, float, list]] = []
    for n in neighbours:
        score = float(n["score"])
        evidence = [{"item": str(n["id"]), "title": n["title"], "similarity": round(score, 3)}]
        if score >= s.dup_threshold and n["type"] == cur["type"]:
            older_first = n["updated_at"] <= cur["updated_at"]
            keep, drop = (n["id"], item_id) if older_first else (item_id, n["id"])
            proposals.append(("merge_items", {"keep": str(keep), "drop": str(drop), "confidence": round(score, 3)},
                              f"near-duplicate (similarity {score:.2f})", score, evidence))
            continue
        if score >= s.link_threshold:
            a, b = sorted([str(item_id), str(n["id"])])
            proposals.append(("add_link", {"src": a, "dst": b, "kind": "related", "confidence": round(score, 3)},
                              f"related content (similarity {score:.2f})", score, evidence))
        # Staleness: a newer item that overlaps an older memory/decision may supersede it.
        if (score >= 0.35 and cur["type"] in FACT_TYPES and n["type"] in ("memory", "decision")
                and n["updated_at"] < cur["updated_at"]):
            old = ctx.conn.execute("select title, body from items_current where id=%s", (n["id"],)).fetchone()
            verdict = ctx.llm.contradiction(f"{old['title']}\n{old['body']}", f"{cur['title']}\n{cur['body']}")
            if verdict.contradicts:
                proposals.append(("mark_stale", {"item_id": str(n["id"]), "superseded_by": str(item_id),
                                                 "explanation": verdict.explanation},
                                  verdict.explanation, verdict.confidence, evidence))

    def commit(conn: Conn) -> dict:
        out = []
        for action, p, reason, conf, ev in proposals:
            r = harness.propose(conn, ctx.agent, action, p, reason=reason, confidence=conf, evidence=ev)
            out.append({"action": action, **{k: str(v) for k, v in r.items()}})
        return {"proposals": out}

    return commit


# ---------------------------------------------------------------- 5. schema evolution

def _infer_type(values: list) -> str | None:
    kinds = {type(v).__name__ for v in values if v is not None}
    if not kinds or kinds & {"dict"}:
        return None
    if kinds <= {"bool"}:
        return "boolean"
    if kinds <= {"int"}:
        return "bigint"
    if kinds <= {"int", "float"}:
        return "double precision"
    if kinds <= {"list"}:
        return "jsonb"
    return "text"


def schema_scan(ctx: Context, payload: dict) -> Commit | None:
    s = ctx.settings
    groups = ctx.conn.execute(
        "select team_id, type, count(*) n from items_current where status='active' group by 1,2 having count(*) >= %s",
        (s.schema_min_items,)).fetchall()
    proposals = []
    for g in groups:
        name = f"{g['type']}_fields"
        if ctx.conn.execute("select 1 from datasets where team_id=%s and name=%s", (g["team_id"], name)).fetchone():
            continue
        counts: Counter = Counter()
        values: dict[str, list] = defaultdict(list)
        for r in ctx.conn.execute("select metadata from items_current where team_id=%s and type=%s and status='active'",
                                  (g["team_id"], g["type"])):
            for k, v in r["metadata"].items():
                if k not in _RESERVED_META and not k.startswith("_"):
                    counts[k] += 1
                    values[k].append(v)
        fields = []
        for k, c in sorted(counts.items()):
            t = _infer_type(values[k])
            if c / g["n"] >= s.schema_min_share and t:
                fields.append({"name": k, "type": t})
        if fields:
            proposals.append({"team_id": str(g["team_id"]), "item_type": g["type"], "dataset_name": name,
                              "fields": fields, "item_count": g["n"]})
    if not proposals:
        return None

    def commit(conn: Conn) -> dict:
        for p in proposals:
            harness.propose(conn, ctx.agent, "promote_schema", p, confidence=None,
                            reason=f"{len(p['fields'])} fields recur across {p['item_count']} {p['item_type']} items",
                            evidence=[{"fields": [f["name"] for f in p["fields"]]}])
        return {"proposed": len(proposals)}

    return commit


# ---------------------------------------------------------------- 6. hygiene

def hygiene(ctx: Context, payload: dict) -> Commit | None:
    removed = gc_orphans(ctx.conn, ctx.blobs) if ctx.blobs is not None else []
    missing = ctx.conn.execute(
        "select c.id from items_current c left join item_embeddings e on e.item_id=c.id and e.model=%s"
        " where c.status='active' and (e.item_id is null) limit 500", (ctx.embedder.name,)).fetchall()

    def commit(conn: Conn) -> dict:
        expired = harness.expire_pending(conn)
        reset = conn.execute("update jobs set status='queued', locked_at=null where status='running'"
                             " and locked_at < now() - interval '15 minutes' returning id").fetchall()
        for r in missing:
            store.enqueue(conn, "enrich", {"item_id": str(r["id"])}, dedupe_key=f"enrich:{r['id']}")
        return {"expired": expired, "orphan_blobs": len(removed), "reembed": len(missing), "stuck_jobs": len(reset)}

    return commit


HANDLERS: dict[str, Callable[[Context, dict], Commit | None]] = {
    "enrich": enrich,
    "relate": relate,
    "schema_scan": schema_scan,
    "hygiene": hygiene,
}
