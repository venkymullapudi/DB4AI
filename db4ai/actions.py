"""Actions the maintainers can propose. Each knows its scope, how to show a diff,
how to apply itself, and how to undo itself (for `auto_notice` reverts).

Apply functions run inside the harness transaction, with `prov.decision_id` set, so the
change and the decision that authorised it commit together.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from uuid import UUID

from . import datasets, store
from .auth import add_member, load_actor
from .db import Conn, Provenance, log_change


@dataclass(frozen=True)
class Action:
    team_of: Callable[[Conn, dict], UUID | None]
    diff: Callable[[Conn, dict], dict]
    apply: Callable[[Conn, Provenance, dict], dict]
    revert: Callable[[Conn, Provenance, dict], None] | None = None


def _team_of_item(key: str) -> Callable[[Conn, dict], UUID | None]:
    def f(conn: Conn, p: dict) -> UUID | None:
        row = conn.execute("select team_id from items where id=%s", (p[key],)).fetchone()
        return row["team_id"] if row else None
    return f


def _cur(conn: Conn, item_id) -> dict:
    row = conn.execute("select * from items_current where id=%s", (item_id,)).fetchone()
    if row is None:
        raise KeyError(f"item {item_id} not found")
    return row


def _meta_action(field: str) -> Action:
    """add_tags / set_summary / set_entities: patch AI metadata on a new version."""

    def diff(conn, p):
        cur = _cur(conn, p["item_id"])
        return {"item": p["item_id"], "title": cur["title"],
                "before": cur["metadata"].get("ai", {}).get(field), "after": p["value"]}

    def apply(conn, prov, p):
        cur = _cur(conn, p["item_id"])
        ai = dict(cur["metadata"].get("ai", {}))
        value = p["value"]
        if field == "tags":
            value = sorted(set(ai.get("tags", [])) | set(value))
        ai[field] = value
        ai["enriched_version"] = p.get("for_version", cur["version"])
        r = store.new_version(conn, prov, p["item_id"], metadata_patch={"ai": ai})
        return {"versions": {str(p["item_id"]): cur["version"]}, "new_version": r["version"]}

    return Action(_team_of_item("item_id"), diff, apply, _revert_versions)


def _revert_versions(conn: Conn, prov: Provenance, result: dict) -> None:
    """Undo = new versions copying the pre-action versions."""
    for item_id, prev in result.get("versions", {}).items():
        old = conn.execute("select * from item_versions where item_id=%s and version=%s", (item_id, prev)).fetchone()
        cur = _cur(conn, item_id)
        store._insert_version(conn, prov, UUID(item_id), cur, title=old["title"], body=old["body"],
                              metadata=old["metadata"], status=old["status"], merged_into=old["merged_into"],
                              blob_sha256=old["blob_sha256"])
    for link_id in result.get("links", []):
        conn.execute("delete from links where id=%s", (link_id,))
        log_change(conn, prov, "link", str(link_id), "delete")


# ---------------------------------------------------------------- links

def _link_diff(conn, p):
    return {"src": _cur(conn, p["src"])["title"], "dst": _cur(conn, p["dst"])["title"], "kind": p.get("kind", "related")}


def _link_apply(conn, prov, p):
    store.add_link(conn, prov, UUID(p["src"]), UUID(p["dst"]), p.get("kind", "related"), p.get("confidence"))
    row = conn.execute("select id from links where (src_item=%s and dst_item=%s or src_item=%s and dst_item=%s) and kind=%s",
                       (p["src"], p["dst"], p["dst"], p["src"], p.get("kind", "related"))).fetchone()
    return {"links": [row["id"]] if row else []}


# ---------------------------------------------------------------- lifecycle

def _status_action(status: str) -> Action:
    def diff(conn, p):
        cur = _cur(conn, p["item_id"])
        return {"item": p["item_id"], "title": cur["title"], "status": [cur["status"], status],
                "superseded_by": p.get("superseded_by")}

    def apply(conn, prov, p):
        cur = _cur(conn, p["item_id"])
        patch = {"stale": {"superseded_by": p.get("superseded_by"), "explanation": p.get("explanation")}} \
            if status == "stale" else None
        store.new_version(conn, prov, p["item_id"], status=status, metadata_patch=patch)
        links = []
        if p.get("superseded_by"):
            store.add_link(conn, prov, UUID(p["superseded_by"]), UUID(p["item_id"]), "supersedes")
            row = conn.execute("select id from links where src_item=%s and dst_item=%s and kind='supersedes'",
                               (p["superseded_by"], p["item_id"])).fetchone()
            links = [row["id"]]
        return {"versions": {str(p["item_id"]): cur["version"]}, "links": links}

    return Action(_team_of_item("item_id"), diff, apply, _revert_versions)


def _merge_diff(conn, p):
    keep, drop = _cur(conn, p["keep"]), _cur(conn, p["drop"])
    return {"keep": {"id": p["keep"], "title": keep["title"]}, "drop": {"id": p["drop"], "title": drop["title"]}}


def _merge_apply(conn, prov, p):
    keep, drop = _cur(conn, p["keep"]), _cur(conn, p["drop"])
    if keep["team_id"] != drop["team_id"]:
        raise ValueError("cross-team merges are not allowed; propose resolve_contradiction instead")
    aliases = sorted(set(keep["metadata"].get("aliases", [])) | {drop["title"]})
    store.new_version(conn, prov, p["keep"], metadata_patch={"aliases": aliases})
    store.new_version(conn, prov, p["drop"], status="merged", merged_into=UUID(p["keep"]))
    store.add_link(conn, prov, UUID(p["drop"]), UUID(p["keep"]), "duplicate_of", p.get("confidence"))
    row = conn.execute("select id from links where src_item=%s and dst_item=%s and kind='duplicate_of'",
                       (p["drop"], p["keep"])).fetchone()
    return {"versions": {str(p["keep"]): keep["version"], str(p["drop"]): drop["version"]}, "links": [row["id"]]}


# ---------------------------------------------------------------- schema evolution

def _promote_diff(conn, p):
    return {"create_table": f"data.<team>_{p['dataset_name']}", "from_items_of_type": p["item_type"],
            "columns": p["fields"], "backfill_items": p.get("item_count")}


def _promote_apply(conn, prov, p):
    team_id = UUID(p["team_id"])
    agent = load_actor(conn, prov.actor_id)
    cols = [{"name": "item_id", "type": "uuid"}] + p["fields"]
    ds = datasets.create_dataset(conn, agent, prov, team_id, p["dataset_name"], cols)
    rows = []
    for it in conn.execute("select id, metadata from items_current where team_id=%s and type=%s and status='active'",
                           (team_id, p["item_type"])):
        r = {"item_id": it["id"]}
        for f in p["fields"]:
            r[datasets.slug(f["name"])] = it["metadata"].get(f["name"])
        rows.append(r)
    n = datasets.insert_rows(conn, agent, prov, ds, rows)
    return {"dataset_id": str(ds["id"]), "table": f"data.{ds['table_name']}", "rows": n}


# ---------------------------------------------------------------- access

def _access_apply(conn, prov, p):
    add_member(conn, UUID(p["team_id"]), UUID(p["actor_id"]), p["role"])
    log_change(conn, prov, "team_member", f"{p['team_id']}:{p['actor_id']}", "upsert", after=p)
    return {}


REGISTRY: dict[str, Action] = {
    "add_tags": _meta_action("tags"),
    "set_summary": _meta_action("summary"),
    "set_entities": _meta_action("entities"),
    "add_link": Action(_team_of_item("src"), _link_diff, _link_apply, _revert_versions),
    "mark_stale": _status_action("stale"),
    "resolve_contradiction": _status_action("stale"),
    "archive_item": _status_action("archived"),
    "merge_items": Action(_team_of_item("keep"), _merge_diff, _merge_apply, _revert_versions),
    "promote_schema": Action(lambda c, p: UUID(p["team_id"]), _promote_diff, _promote_apply, None),
    "change_access": Action(lambda c, p: UUID(p["team_id"]), lambda c, p: dict(p), _access_apply, None),
}
