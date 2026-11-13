"""Connections, transactions with provenance, and the migration runner."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from importlib import resources
from typing import Iterator
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import settings

Conn = psycopg.Connection


@dataclass(frozen=True)
class Provenance:
    """Who is making a change and why. Recorded on every version and change_log row."""

    actor_id: UUID
    source: str
    reason: str | None = None
    decision_id: UUID | None = None


def connect(url: str | None = None) -> Conn:
    return psycopg.connect(url or settings().database_url, row_factory=dict_row, autocommit=True)


@contextlib.contextmanager
def transaction(conn: Conn, prov: Provenance | None = None) -> Iterator[Conn]:
    """One ACID transaction. Sets provenance for change-capture triggers on `data.*` tables."""
    with conn.transaction():
        if prov is not None:
            set_provenance(conn, prov)
        yield conn


def set_provenance(conn: Conn, prov: Provenance) -> None:
    conn.execute(
        "select set_config('db4ai.actor', %s, true), set_config('db4ai.source', %s, true),"
        " set_config('db4ai.reason', %s, true), set_config('db4ai.decision', %s, true)",
        (str(prov.actor_id), prov.source, prov.reason or "", str(prov.decision_id or "")),
    )


def log_change(conn: Conn, prov: Provenance, entity: str, entity_id: str | None, op: str,
               before: dict | None = None, after: dict | None = None) -> None:
    conn.execute(
        "insert into change_log (actor_id, source, reason, harness_decision_id, entity, entity_id, op, before, after)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (prov.actor_id, prov.source, prov.reason, prov.decision_id, entity, entity_id, op,
         Jsonb(before) if before is not None else None, Jsonb(after) if after is not None else None),
    )


# ---------------------------------------------------------------- migrations

def has_pgvector(conn: Conn) -> bool:
    row = conn.execute("select 1 from pg_available_extensions where name = 'vector'").fetchone()
    return row is not None


def vector_enabled(conn: Conn) -> bool:
    row = conn.execute(
        "select format_type(atttypid, atttypmod) as t from pg_attribute"
        " where attrelid = 'item_embeddings'::regclass and attname = 'embedding'"
    ).fetchone()
    return row is not None and row["t"].startswith("vector")


def migrate(conn: Conn) -> list[str]:
    """Apply pending SQL migrations in order. Returns the names applied."""
    conn.execute("create table if not exists schema_migrations (name text primary key, applied_at timestamptz default now())")
    applied = {r["name"] for r in conn.execute("select name from schema_migrations")}
    files = sorted((p for p in resources.files("db4ai.migrations").iterdir() if p.name.endswith(".sql")), key=lambda p: p.name)
    done: list[str] = []
    for f in files:
        if f.name in applied:
            continue
        sql = f.read_text()
        if "{{EMBEDDING_TYPE}}" in sql:
            if has_pgvector(conn):
                conn.execute("create extension if not exists vector")
                sql = sql.replace("{{EMBEDDING_TYPE}}", "vector")
            else:
                sql = sql.replace("{{EMBEDDING_TYPE}}", "float8[]")
        with conn.transaction():
            conn.execute(sql)
            conn.execute("insert into schema_migrations (name) values (%s)", (f.name,))
        done.append(f.name)
    return done
