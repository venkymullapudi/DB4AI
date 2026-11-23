"""High-level operations shared by the HTTP API, the MCP server and the CLI."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from . import ask as ask_mod
from . import connectors, datasets, erasure, harness, store
from .auth import Actor, team_id_by_name
from .db import Conn, Provenance, transaction
from .embeddings import get_embedder
from .llm import get_llm


def resolve_team(conn: Conn, actor: Actor, team: str | None) -> UUID:
    if team:
        tid = team_id_by_name(conn, team)
    elif len(actor.teams) == 1:
        tid = next(iter(actor.teams))
    else:
        raise ValueError("team is required (you belong to several teams)")
    actor.require_write(tid)
    return tid


def save(conn: Conn, actor: Actor, *, type: str, title: str, body: str = "", team: str | None = None,
         metadata: dict | None = None, external_key: str | None = None, source: str = "api",
         reason: str | None = None) -> dict:
    tid = resolve_team(conn, actor, team)
    prov = Provenance(actor.id, source, reason)
    with transaction(conn, prov):
        r = store.save_item(conn, actor, prov, team_id=tid, type=type, title=title, body=body,
                            metadata=metadata, external_key=external_key)
    return jsonable(r)


def sync_cursor(conn: Conn, actor: Actor, source: str, team: str | None = None) -> dict:
    return jsonable(connectors.get_cursor(conn, actor, source, resolve_team(conn, actor, team)))


def sync_batch(conn: Conn, actor: Actor, source: str, *, items: list[dict], cursor: str | None,
               expected_cursor: str | None, team: str | None = None, deleted: list[str] | None = None,
               datasets: list[dict] | None = None, deleted_datasets: list[str] | None = None) -> dict:
    records = [connectors.Record(key=i["key"], type=i["type"], title=i["title"], body=i.get("body", ""),
                                 metadata=i.get("metadata") or {}) for i in items]
    snapshots = [connectors.DatasetRecord(key=d["key"], name=d["name"], columns=d["columns"], rows=d.get("rows") or [])
                 for d in datasets or []]
    return jsonable(connectors.apply_batch(conn, actor, source, resolve_team(conn, actor, team), records,
                                           cursor, expected_cursor, deleted, snapshots, deleted_datasets))


def search(conn: Conn, actor: Actor, query: str, *, types: list[str] | None = None, limit: int = 10) -> list[dict]:
    return jsonable(store.search(conn, actor, query, embedder=get_embedder(), types=types, limit=limit))


def ask(conn: Conn, actor: Actor, question: str, *, types: list[str] | None = None, limit: int = 8,
        llm=None) -> dict:
    """Answer from items the actor can read, citing item ids and versions. Logged if DB4AI_ASK_LOG is on."""
    return jsonable(ask_mod.ask(conn, actor, question, llm=llm or get_llm(), embedder=get_embedder(),
                                types=types, limit=limit))


def ask_log(conn: Conn, actor: Actor, *, actor_name: str | None = None, item_id: str | None = None,
            limit: int = 50) -> list[dict]:
    return jsonable(ask_mod.log(conn, actor, actor_name=actor_name, item_id=UUID(item_id) if item_id else None,
                                limit=limit))


def get(conn: Conn, actor: Actor, item_id: str) -> dict:
    return jsonable(store.get_item(conn, actor, UUID(item_id)))


def history(conn: Conn, actor: Actor, item_id: str) -> list[dict]:
    return jsonable(store.history(conn, actor, UUID(item_id)))


def revert(conn: Conn, actor: Actor, item_id: str, to_version: int, reason: str | None = None, source: str = "api") -> dict:
    prov = Provenance(actor.id, source, reason or f"revert to v{to_version}")
    with transaction(conn, prov):
        return jsonable(store.revert(conn, actor, prov, UUID(item_id), to_version))


def request_erasure(conn: Conn, actor: Actor, item_id: str, reason: str, terms: list[str] | None = None,
                    source: str = "api") -> dict:
    """Queue an erasure (no terms) or redaction (terms) for an admin to approve in the harness."""
    with transaction(conn):
        return jsonable(erasure.request(conn, actor, UUID(item_id), reason=reason, terms=terms, source=source))


def erasures(conn: Conn, actor: Actor, item_id: str) -> list[dict]:
    return jsonable(erasure.erasures(conn, actor, UUID(item_id)))


def pending(conn: Conn, actor: Actor, status: str = "pending") -> list[dict]:
    return jsonable(harness.list_decisions(conn, actor, status))


def decide(conn: Conn, actor: Actor, decision_id: str, approve: bool, note: str | None = None,
           edited_payload: dict | None = None) -> dict:
    return jsonable(harness.decide(conn, actor, UUID(decision_id), approve, note, edited_payload))


def revert_decision(conn: Conn, actor: Actor, decision_id: str, note: str | None = None) -> dict:
    return jsonable(harness.revert(conn, actor, UUID(decision_id), note))


def dataset_rows(conn: Conn, actor: Actor, name: str, team: str | None = None, limit: int = 100) -> list[dict]:
    tid = team_id_by_name(conn, team) if team else resolve_team(conn, actor, None)
    return jsonable(datasets.query(conn, actor, tid, name, limit))


def jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if isinstance(v, UUID):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    return v
