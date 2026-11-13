"""Structured datasets: real tables in schema `data`, so BI tools (Tableau, Metabase…) can query them.

Every row change is captured into the append-only change_log by a trigger, with the
provenance the app sets on the transaction.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from uuid import UUID

from psycopg import sql
from psycopg.types.json import Jsonb

from .auth import AccessDenied, Actor
from .db import Conn, Provenance, log_change

_IDENT = re.compile(r"[^a-z0-9_]+")
TYPES = {"bigint", "double precision", "boolean", "timestamptz", "text", "jsonb", "uuid"}


def slug(name: str) -> str:
    s = _IDENT.sub("_", name.lower()).strip("_") or "dataset"
    return ("t_" + s if s[0].isdigit() else s)[:50]


def _infer(values: list[str]) -> str:
    vals = [v for v in values if v not in ("", None)]
    if not vals:
        return "text"

    def all_(fn) -> bool:
        try:
            return all(fn(v) is not False for v in vals)
        except (ValueError, TypeError):
            return False

    if all_(lambda v: int(v) if re.fullmatch(r"-?\d+", v) else False):
        return "bigint"
    if all_(float):
        return "double precision"
    if all(v.lower() in ("true", "false", "yes", "no") for v in vals):
        return "boolean"
    if all_(lambda v: datetime.fromisoformat(v)):
        return "timestamptz"
    return "text"


def _convert(v, typ: str):
    if v in ("", None):
        return None
    if typ == "boolean" and isinstance(v, str):
        return v.lower() in ("true", "yes")
    if typ == "jsonb":
        return Jsonb(v)
    return v


def get_dataset(conn: Conn, team_id: UUID, name: str) -> dict | None:
    return conn.execute("select * from datasets where team_id=%s and name=%s", (team_id, name)).fetchone()


def create_dataset(conn: Conn, actor: Actor, prov: Provenance, team_id: UUID, name: str,
                   columns: list[dict]) -> dict:
    """Create data.<team>_<name> with the given [{name, type}] columns."""
    actor.require_write(team_id)
    existing = get_dataset(conn, team_id, name)
    if existing:
        return existing
    team = conn.execute("select name from teams where id=%s", (team_id,)).fetchone()["name"]
    table = slug(f"{team}_{name}")
    cols = []
    for c in columns:
        if c["type"] not in TYPES:
            raise ValueError(f"unsupported column type {c['type']!r}")
        cols.append({"name": slug(c["name"]), "type": c["type"]})
    col_sql = sql.SQL(", ").join(
        sql.SQL("{} {}").format(sql.Identifier(c["name"]), sql.SQL(c["type"])) for c in cols)
    conn.execute(sql.SQL("create table data.{} (_row_id bigserial primary key, {},"
                         " _created_at timestamptz not null default now())").format(sql.Identifier(table), col_sql))
    conn.execute(sql.SQL("create trigger capture after insert or update or delete on data.{}"
                         " for each row execute function db4ai_capture_change()").format(sql.Identifier(table)))
    row = conn.execute(
        "insert into datasets (team_id, name, table_name, columns, created_by, harness_decision_id)"
        " values (%s,%s,%s,%s,%s,%s) returning *",
        (team_id, name, table, Jsonb(cols), actor.id, prov.decision_id),
    ).fetchone()
    log_change(conn, prov, "dataset", str(row["id"]), "create", after={"table": f"data.{table}", "columns": cols})
    return row


def insert_rows(conn: Conn, actor: Actor, prov: Provenance, dataset: dict, rows: list[dict]) -> int:
    actor.require_write(dataset["team_id"])
    cols = dataset["columns"]
    if not rows:
        return 0
    names = [c["name"] for c in cols]
    stmt = sql.SQL("insert into data.{} ({}) values ({})").format(
        sql.Identifier(dataset["table_name"]),
        sql.SQL(", ").join(map(sql.Identifier, names)),
        sql.SQL(", ").join(sql.Placeholder() * len(names)),
    )
    with conn.cursor() as cur:
        cur.executemany(stmt, [[_convert(r.get(c["name"]), c["type"]) for c in cols] for r in rows])
    return len(rows)


def import_csv(conn: Conn, actor: Actor, prov: Provenance, team_id: UUID, name: str, text: str) -> dict:
    reader = csv.DictReader(io.StringIO(text))
    raw = list(reader)
    fields = reader.fieldnames or []
    mapping = {f: slug(f) for f in fields}
    rows = [{mapping[k]: v for k, v in r.items() if k in mapping} for r in raw]
    ds = get_dataset(conn, team_id, name)
    if ds is None:
        columns = [{"name": mapping[f], "type": _infer([r[f] for r in raw])} for f in fields]
        ds = create_dataset(conn, actor, prov, team_id, name, columns)
    n = insert_rows(conn, actor, prov, ds, rows)
    return {"dataset": name, "table": f"data.{ds['table_name']}", "rows": n}


def query(conn: Conn, actor: Actor, team_id: UUID, name: str, limit: int = 100) -> list[dict]:
    actor.require_read(team_id)
    ds = get_dataset(conn, team_id, name)
    if ds is None:
        raise KeyError(f"no dataset {name!r}")
    return conn.execute(sql.SQL("select * from data.{} order by _row_id limit %s").format(
        sql.Identifier(ds["table_name"])), (limit,)).fetchall()


def run_readonly_sql(conn: Conn, actor: Actor, statement: str, limit: int = 500) -> list[dict]:
    """Ad-hoc analytics SQL. Admin only, in a READ ONLY transaction."""
    if not actor.is_admin:
        raise AccessDenied("raw SQL is admin-only; use dataset queries")
    with conn.transaction():
        conn.execute("set transaction read only")
        conn.execute("set local statement_timeout = '30s'")
        cur = conn.execute(statement)
        return cur.fetchmany(limit) if cur.description else []
