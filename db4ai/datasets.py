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


# ---------------------------------------------------------------- snapshots from connectors

MAX_ROWS = 100_000
MAX_COLUMNS = 200


def _fits(v, typ: str) -> bool:
    """Can the source value `v` be stored in a column of type `typ` (as `_convert` would store it)?"""
    if v in ("", None) or typ in ("text", "jsonb"):
        return True
    v = str(v)
    try:
        if typ == "bigint":
            return bool(re.fullmatch(r"-?\d{1,18}", v))
        if typ == "double precision":
            float(v)
            return True
        if typ == "boolean":
            return v.lower() in ("true", "false", "yes", "no")
        if typ == "timestamptz":
            datetime.fromisoformat(v)
            return True
        if typ == "uuid":
            UUID(v)
            return True
    except (ValueError, TypeError):
        return False
    return False


def snapshot_header(columns: list[str]) -> list[str]:
    """Source column names → unique column slugs (a repeated name gets _2, _3...)."""
    out: list[str] = []
    for c in columns:
        base = slug(str(c or "column"))
        name, n = base, 1
        while name in out or name in ("_row_id", "_created_at"):
            n += 1
            name = f"{base[:45]}_{n}"
        out.append(name)
    return out


def check_snapshot(columns: list[str], rows: list[list]) -> None:
    if not columns:
        raise ValueError("a dataset snapshot needs at least one column")
    if len(columns) > MAX_COLUMNS:
        raise ValueError(f"at most {MAX_COLUMNS} columns per dataset")
    if len(rows) > MAX_ROWS:
        raise ValueError(f"at most {MAX_ROWS} rows per dataset snapshot")
    if any(len(r) > len(columns) for r in rows):
        raise ValueError("a snapshot row has more cells than the header")


def drift(ds: dict, header: list[str], rows: list[list]) -> dict:
    """What `alter_dataset` must change before this snapshot can be applied: {add, retype, retire}."""
    cols = {c["name"]: c for c in ds["columns"]}
    synced = [c["name"] for c in ds["columns"] if not c.get("retired") and c["name"] != "item_id"]
    add, retype = [], []
    for i, h in enumerate(header):
        values = [r[i] if i < len(r) else None for r in rows]
        c = cols.get(h)
        if c is None:
            add.append({"name": h, "type": _infer([v for v in values if v is not None])})
        elif c.get("retired"):  # back in the source; if its values no longer fit, it comes back as text
            fits = c["type"] == "text" or all(_fits(v, c["type"]) for v in values)
            add.append({"name": h, "type": c["type"] if fits else "text"})
        elif c["type"] != "text" and not all(_fits(v, c["type"]) for v in values):
            retype.append(h)
    retire = [n for n in synced if n not in header]
    return {"add": add, "retype": retype, "retire": retire}


def apply_snapshot(conn: Conn, ds: dict, header: list[str], rows: list[list]) -> dict:
    """Diff the snapshot into data.<table>: delete rows the source no longer has, insert new ones. Rows are compared
    on the synced (non-retired) columns as a multiset, so an unchanged snapshot writes nothing and change_log only
    records real changes. Retired columns keep their old values on unchanged rows and are null on new ones."""
    types = {c["name"]: c["type"] for c in ds["columns"]}
    table = sql.Identifier(ds["table_name"])
    cols = [sql.Identifier(h) for h in header]
    col_list = sql.SQL(", ").join(cols)
    snap = sql.Identifier(f"_snap_{ds['id'].hex[:12]}")
    conn.execute(sql.SQL("create temp table {} on commit drop as select {} from data.{} limit 0").format(
        snap, col_list, table))
    conn.execute(sql.SQL("alter table {} add column _ord int").format(snap))
    stmt = sql.SQL("insert into {} ({}, _ord) values ({})").format(
        snap, col_list, sql.SQL(", ").join(sql.Placeholder() * (len(header) + 1)))
    with conn.cursor() as cur:
        cur.executemany(stmt, [[_convert(r[i] if i < len(r) else None, types[h]) for i, h in enumerate(header)] + [n]
                               for n, r in enumerate(rows)])
    match = sql.SQL(" and ").join(sql.SQL("n.{0} is not distinct from o.{0}").format(c) for c in cols)
    res = conn.execute(sql.SQL(
        "with o as (select _row_id, row_number() over (partition by {cols} order by _row_id) rn, {cols} from data.{t}),"
        " n as (select row_number() over (partition by {cols} order by _ord) rn, _ord, {cols} from {s}),"
        " del as (delete from data.{t} where _row_id in (select o._row_id from o where not exists"
        "   (select 1 from n where n.rn = o.rn and {m})) returning 1),"
        " ins as (insert into data.{t} ({cols}) select {ncols} from n where not exists"
        "   (select 1 from o where o.rn = n.rn and {m}) order by n._ord returning 1)"
        " select (select count(*) from del) as deleted, (select count(*) from ins) as inserted").format(
        cols=col_list, t=table, s=snap, m=match,
        ncols=sql.SQL(", ").join(sql.SQL("n.{}").format(c) for c in cols))).fetchone()
    conn.execute(sql.SQL("drop table {}").format(snap))
    return {"inserted": res["inserted"], "deleted": res["deleted"]}


def alter(conn: Conn, dataset_id, add: list[dict], retype: list[str], retire: list[str]) -> dict:
    """Schema change for a synced dataset (applied by the `alter_dataset` harness action). Never drops a column:
    a column the source removed is only marked retired, so its existing values stay queryable."""
    ds = conn.execute("select * from datasets where id=%s for update", (dataset_id,)).fetchone()
    if ds is None:
        raise KeyError(f"no dataset {dataset_id}")
    table = sql.Identifier(ds["table_name"])
    cols = [dict(c) for c in ds["columns"]]
    by_name = {c["name"]: c for c in cols}
    for a in add:
        name, typ = a["name"], a["type"]
        if name != slug(name) or typ not in TYPES:
            raise ValueError(f"bad column {a!r}")
        c = by_name.get(name)
        if c is None:
            conn.execute(sql.SQL("alter table data.{} add column {} {}").format(
                table, sql.Identifier(name), sql.SQL(typ)))
            c = {"name": name, "type": typ}
            cols.append(c)
            by_name[name] = c
        else:
            c.pop("retired", None)
            if c["type"] != typ:
                retype = [*retype, name] if typ == "text" else retype
    for name in dict.fromkeys(retype):
        c = by_name.get(name)
        if c is None:
            raise ValueError(f"no column {name!r}")
        if c["type"] != "text":
            conn.execute(sql.SQL("alter table data.{0} alter column {1} type text using {1}::text").format(
                table, sql.Identifier(name)))
            c["type"] = "text"
    for name in retire:
        if name in by_name and name != "item_id":
            by_name[name]["retired"] = True
    conn.execute("update datasets set columns=%s where id=%s", (Jsonb(cols), dataset_id))
    return {"dataset_id": str(dataset_id), "table": f"data.{ds['table_name']}", "columns": cols}
