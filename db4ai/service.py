"""High-level operations shared by the HTTP API, the MCP server and the CLI."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from . import datasets, harness, store
from .auth import Actor, team_id_by_name
from .db import Conn, Provenance, transaction
from .embeddings import get_embedder


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


def search(conn: Conn, actor: Actor, query: str, *, types: list[str] | None = None, limit: int = 10) -> list[dict]:
    return jsonable(store.search(conn, actor, query, embedder=get_embedder(), types=types, limit=limit))


def get(conn: Conn, actor: Actor, item_id: str) -> dict:
    return jsonable(store.get_item(conn, actor, UUID(item_id)))


def history(conn: Conn, actor: Actor, item_id: str) -> list[dict]:
    return jsonable(store.history(conn, actor, UUID(item_id)))


def revert(conn: Conn, actor: Actor, item_id: str, to_version: int, reason: str | None = None, source: str = "api") -> dict:
    prov = Provenance(actor.id, source, reason or f"revert to v{to_version}")
    with transaction(conn, prov):
        return jsonable(store.revert(conn, actor, prov, UUID(item_id), to_version))


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
