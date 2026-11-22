"""Redaction and erasure via the AIDBHarness (SPEC §3, §5).

Two actions, both always `ask_first` and decided only by an admin:

* `erase_item`  {item_id, reason} — scrub the item's content from every version, its change_log rows and the
  harness decisions that mention it; delete its embeddings, chunks and (unshared) blobs; then add a final
  `erased` version. The item id, version numbers and provenance stay, so history still shows who did what.
* `redact_item` {item_id, reason, terms} — replace each term (case-insensitive) with `[redacted]` in the same
  places, keeping everything else; the item is re-enriched from the redacted content.

Both write an `erasures` tombstone row (what kind, how many versions, by whom, why) that never holds the
erased content. This is the only sanctioned bypass of the append-only triggers: the triggers let the content
columns of *this decision's item* change while `db4ai.erasure` names the pending decision being applied.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from . import store
from .auth import AccessDenied, Actor
from .db import Conn, Provenance, log_change

ACTIONS = ("erase_item", "redact_item")
ERASED = "[erased]"
REDACTED = "[redacted]"
ERASED_KEY_PREFIX = "erased:"
# Payload keys that only hold ids/numbers; everything else in a payload may carry content.
_ID_KEYS = {"item_id", "src", "dst", "keep", "drop", "superseded_by", "kind", "confidence", "for_version",
            "team_id"}
_ITEM_REFS = ("item_id", "src", "dst", "keep", "drop", "superseded_by")


def withheld_key(external_key: str) -> str:
    """What an erased item's external key becomes: re-imports of the same key are skipped, not resurrected."""
    return ERASED_KEY_PREFIX + hashlib.sha256(external_key.encode()).hexdigest()


# ---------------------------------------------------------------- requests

def request(conn: Conn, actor: Actor, item_id: UUID, *, reason: str, terms: list[str] | None = None,
            source: str = "api") -> dict:
    """Ask for an erasure (no terms) or a redaction (terms). Queued in the harness for an admin.

    Must be called inside a transaction.
    """
    from . import harness  # harness imports actions, which imports this module

    team = conn.execute("select team_id from items where id = %s", (item_id,)).fetchone()
    if team is None:
        raise KeyError(f"item {item_id} not found")
    if not actor.can_read(team["team_id"]):
        raise AccessDenied(f"{actor.name} cannot access item {item_id}")
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("a reason is required for erasure and redaction requests")
    payload: dict[str, Any] = {"item_id": str(item_id), "reason": reason}
    action = "erase_item"
    if terms is not None:
        payload["terms"] = _clean_terms(terms)
        action = "redact_item"
    return harness.propose(conn, actor, action, payload, reason=reason, source=source)


def _clean_terms(terms: list[str]) -> list[str]:
    out = sorted({t.strip() for t in terms if t and t.strip()}, key=lambda t: (-len(t), t))
    if not out:
        raise ValueError("redaction needs at least one non-empty term")
    return out


# ---------------------------------------------------------------- scrubbing helpers

def _pattern(terms: list[str]) -> re.Pattern:
    # Longest first, so a term containing another is replaced whole.
    return re.compile("|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)), re.IGNORECASE)


def _replace(v: Any, pat: re.Pattern, counter: list[int]) -> Any:
    """Deep-replace matches in every string (dict keys included) of a JSON-like value."""
    if isinstance(v, str):
        new, n = pat.subn(REDACTED, v)
        counter[0] += n
        return new
    if isinstance(v, dict):
        return {_replace(k, pat, counter): _replace(x, pat, counter) for k, x in v.items()}
    if isinstance(v, list):
        return [_replace(x, pat, counter) for x in v]
    return v


def _jsonb(v: Any) -> Jsonb | None:
    return None if v is None else Jsonb(v)


def _versions(conn: Conn, item_id: UUID) -> list[dict]:
    return conn.execute("select * from item_versions where item_id = %s order by version", (item_id,)).fetchall()


def _decisions_about(conn: Conn, item_id: UUID) -> list[dict]:
    cond = " or ".join(f"payload ->> '{k}' = %(id)s" for k in _ITEM_REFS)
    return conn.execute(f"select * from harness_decisions where {cond}", {"id": str(item_id)}).fetchall()


def _count(conn: Conn, item_id: UUID, pat: re.Pattern) -> int:
    counter = [0]
    for v in _versions(conn, item_id):
        _replace([v["title"], v["body"], v["metadata"], v["reason"]], pat, counter)
    return counter[0]


# ---------------------------------------------------------------- diffs (shown to the admin)

def _cur(conn: Conn, item_id) -> dict:
    row = conn.execute("select * from items_current where id = %s", (item_id,)).fetchone()
    if row is None:
        raise KeyError(f"item {item_id} not found")
    return row


def _erase_diff(conn: Conn, p: dict) -> dict:
    cur = _cur(conn, p["item_id"])
    blobs = conn.execute("select count(distinct blob_sha256) n from item_versions where item_id = %s",
                         (p["item_id"],)).fetchone()["n"]
    return {"item": p["item_id"], "title": cur["title"], "erase": "all content of every version",
            "versions": cur["version"], "blobs": blobs,
            "decisions_mentioning_item": len(_decisions_about(conn, UUID(p["item_id"]))),
            "reason": p.get("reason")}


def _redact_diff(conn: Conn, p: dict) -> dict:
    cur = _cur(conn, p["item_id"])
    terms = _clean_terms(p.get("terms") or [])
    return {"item": p["item_id"], "title": cur["title"], "replace_with": REDACTED,
            "terms": {t: _count(conn, UUID(p["item_id"]), _pattern([t])) for t in terms},
            "versions": cur["version"], "reason": p.get("reason")}


# ---------------------------------------------------------------- apply

def _begin(conn: Conn, prov: Provenance, p: dict) -> tuple[UUID, dict, dict]:
    if prov.decision_id is None:
        raise ValueError("erasure must be applied through an approved harness decision")
    item_id = UUID(p["item_id"])
    cur = _cur(conn, item_id)
    if cur["status"] == "erased":
        raise ValueError(f"item {item_id} is already erased")
    decision = conn.execute("select * from harness_decisions where id = %s", (prov.decision_id,)).fetchone()
    conn.execute("select id from items where id = %s for update", (item_id,))  # serialise with other writers
    conn.execute("select set_config('db4ai.erasure', %s, true)", (str(prov.decision_id),))
    return item_id, cur, decision


def _end(conn: Conn) -> None:
    conn.execute("select set_config('db4ai.erasure', '', true)")


def _drop_derived(conn: Conn, item_id: UUID) -> None:
    """Embeddings and chunks were computed from the old content."""
    conn.execute("delete from item_embeddings where item_id = %s", (item_id,))
    conn.execute("delete from item_chunks where item_id = %s", (item_id,))


def _tombstone(conn: Conn, prov: Provenance, item_id: UUID, decision: dict, kind: str, reason: str,
               **counts) -> UUID:
    row = conn.execute(
        "insert into erasures (item_id, decision_id, kind, requested_by, approved_by, reason, versions, terms,"
        " matches, blobs_deleted, blobs_kept) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id",
        (item_id, decision["id"], kind, decision["proposed_by"], prov.actor_id,
         reason, counts.get("versions", 0),
         counts.get("terms", 0), counts.get("matches", 0), counts.get("blobs_deleted", 0),
         counts.get("blobs_kept", 0)),
    ).fetchone()
    return row["id"]


def _reason(decision: dict) -> str:
    return decision["payload"].get("reason") or decision["reason"] or decision["action_type"]


def _erase_apply(conn: Conn, prov: Provenance, p: dict) -> dict:
    item_id, cur, decision = _begin(conn, prov, p)
    try:
        versions = _versions(conn, item_id)
        shas = sorted({v["blob_sha256"] for v in versions if v["blob_sha256"]})
        chash = store.content_hash(ERASED, "", None)
        conn.execute(
            "update item_versions set title = %s, body = '', metadata = '{}', blob_sha256 = null,"
            " content_hash = %s, reason = case when reason is null then null else %s end where item_id = %s",
            (ERASED, chash, ERASED, item_id))
        conn.execute(
            "update change_log set before = before - array['title', 'content_hash'],"
            " after = after - array['title', 'content_hash'],"
            " reason = case when reason is null then null else %s end"
            " where entity = 'item' and entity_id = %s", (ERASED, str(item_id)))
        key = conn.execute("select external_key from items where id = %s", (item_id,)).fetchone()["external_key"]
        if key is not None and not key.startswith(ERASED_KEY_PREFIX):
            conn.execute("update items set external_key = %s where id = %s", (withheld_key(key), item_id))

        # Harness decisions that mention the item keep only ids; their diffs, evidence and notes go.
        for d in _decisions_about(conn, item_id):
            if d["id"] == decision["id"]:
                continue
            conn.execute("update change_log set reason = %s where harness_decision_id = %s and reason is not null",
                         (ERASED, d["id"]))
            conn.execute(
                "update harness_decisions set payload = %s, diff = %s, evidence = '[]',"
                " reason = case when reason is null then null else %s end,"
                " decision_note = case when decision_note is null then null else %s end where id = %s",
                (Jsonb({k: v for k, v in d["payload"].items() if k in _ID_KEYS}),
                 Jsonb({"erased_item": str(item_id)}), ERASED, ERASED, d["id"]))
        conn.execute("update links set reason = %s where (src_item = %s or dst_item = %s) and reason is not null",
                     (ERASED, item_id, item_id))
        _drop_derived(conn, item_id)

        # Blobs: delete the row now (so nothing points at it after commit); the file goes via the outbox.
        deleted = kept = 0
        for sha in shas:
            if conn.execute("select 1 from item_versions where blob_sha256 = %s limit 1", (sha,)).fetchone():
                kept += 1  # same bytes are another item's content too
                continue
            conn.execute("delete from blobs where sha256 = %s", (sha,))
            store.enqueue(conn, "blob_delete", {"sha256": sha}, dedupe_key=f"blob_delete:{sha}")
            deleted += 1

        eid = _tombstone(conn, prov, item_id, decision, "erase", _reason(decision), versions=len(versions),
                         blobs_deleted=deleted, blobs_kept=kept)
        conn.execute("update harness_decisions set diff = %s where id = %s",
                     (Jsonb({"item": str(item_id), "erased": True, "versions": len(versions), "erasure_id": str(eid)}),
                      decision["id"]))
    finally:
        _end(conn)
    base = _cur(conn, item_id)
    r = store._insert_version(conn, prov, item_id, base, title=ERASED, body="",
                              metadata={"erasure": {"id": str(eid), "decision_id": str(decision["id"])}},
                              status="erased")
    log_change(conn, prov, "item", str(item_id), "erase", after={"erasure_id": str(eid), "versions": len(versions)})
    return {"erasure_id": str(eid), "versions_scrubbed": len(versions), "new_version": r["version"],
            "blobs_deleted": deleted, "blobs_kept": kept}


def _redact_apply(conn: Conn, prov: Provenance, p: dict) -> dict:
    item_id, cur, decision = _begin(conn, prov, p)
    terms = _clean_terms(p.get("terms") or [])
    pat = _pattern(terms)
    counter = [0]
    try:
        versions = _versions(conn, item_id)
        matches = _count(conn, item_id, pat)  # occurrences in the item's own versions
        for v in versions:
            title, body = _replace(v["title"], pat, counter), _replace(v["body"], pat, counter)
            meta, reason = _replace(v["metadata"], pat, counter), _replace(v["reason"], pat, counter)
            if (title, body, meta, reason) != (v["title"], v["body"], v["metadata"], v["reason"]):
                conn.execute(
                    "update item_versions set title = %s, body = %s, metadata = %s, reason = %s, content_hash = %s"
                    " where id = %s",
                    (title, body, Jsonb(meta), reason, store.content_hash(title, body, v["blob_sha256"]), v["id"]))
        ids = [d["id"] for d in _decisions_about(conn, item_id)]
        for row in conn.execute("select id, before, after, reason from change_log where (entity = 'item'"
                                " and entity_id = %s) or harness_decision_id = any(%s)",
                                (str(item_id), ids)).fetchall():
            before = _replace(row["before"], pat, counter)
            after = _replace(row["after"], pat, counter)
            if isinstance(before, dict):
                before.pop("content_hash", None)
            if isinstance(after, dict):
                after.pop("content_hash", None)
            conn.execute("update change_log set before = %s, after = %s, reason = %s where id = %s",
                         (_jsonb(before), _jsonb(after), _replace(row["reason"], pat, counter), row["id"]))
        key = conn.execute("select external_key from items where id = %s", (item_id,)).fetchone()["external_key"]
        if key is not None and pat.search(key):
            conn.execute("update items set external_key = %s where id = %s", (withheld_key(key), item_id))

        # Harness decisions that mention the item, including this one (its terms are content too).
        for d in _decisions_about(conn, item_id):
            fields = {f: _replace(d[f], pat, counter)
                      for f in ("payload", "diff", "evidence", "result", "reason", "decision_note")}
            conn.execute(
                "update harness_decisions set payload = %s, diff = %s, evidence = %s, result = %s, reason = %s,"
                " decision_note = %s where id = %s",
                (Jsonb(fields["payload"]), Jsonb(fields["diff"]), Jsonb(fields["evidence"]),
                 _jsonb(fields["result"]), fields["reason"], fields["decision_note"], d["id"]))
        for row in conn.execute("select id, reason from links where (src_item = %s or dst_item = %s)"
                                " and reason is not null", (item_id, item_id)).fetchall():
            conn.execute("update links set reason = %s where id = %s", (_replace(row["reason"], pat, counter), row["id"]))
        _drop_derived(conn, item_id)
    finally:
        _end(conn)
    # The request's own reason may quote a term; nothing written after the scrub may carry one.
    prov = Provenance(prov.actor_id, prov.source, _replace(prov.reason, pat, [0]), prov.decision_id)
    eid = _tombstone(conn, prov, item_id, decision, "redact", _replace(_reason(decision), pat, [0]),
                     versions=len(versions), terms=len(terms), matches=matches)
    base = _cur(conn, item_id)
    history = list(base["metadata"].get("redactions", []))
    history.append({"id": str(eid), "decision_id": str(decision["id"]), "terms": len(terms)})
    r = store.new_version(conn, prov, item_id, metadata_patch={"redactions": history})
    store.enqueue(conn, "enrich", {"item_id": str(item_id)}, dedupe_key=f"enrich:{item_id}")
    log_change(conn, prov, "item", str(item_id), "redact", after={"erasure_id": str(eid), "terms": len(terms)})
    return {"erasure_id": str(eid), "versions_scrubbed": len(versions), "matches": matches,
            "new_version": r["version"]}


def erasures(conn: Conn, actor: Actor, item_id: UUID) -> list[dict]:
    team = conn.execute("select team_id from items where id = %s", (item_id,)).fetchone()
    if team is None:
        raise KeyError(f"item {item_id} not found")
    actor.require_read(team["team_id"])
    return conn.execute(
        "select e.id, e.kind, e.reason, e.versions, e.terms, e.matches, e.blobs_deleted, e.blobs_kept,"
        " e.decision_id, e.created_at, r.name as requested_by, a.name as approved_by"
        " from erasures e join actors r on r.id = e.requested_by join actors a on a.id = e.approved_by"
        " where e.item_id = %s order by e.created_at", (item_id,)).fetchall()
