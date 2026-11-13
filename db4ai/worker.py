"""Job worker over the Postgres queue (`FOR UPDATE SKIP LOCKED`). Run several for throughput."""

from __future__ import annotations

import logging
import time

from . import store
from .db import Conn
from .maintainers import HANDLERS, Context

log = logging.getLogger(__name__)
PERIODIC = {"schema_scan": 3600, "hygiene": 600}  # seconds


def claim(conn: Conn) -> dict | None:
    return conn.execute(
        "update jobs set status='running', locked_at=now(), attempts=attempts+1"
        " where id = (select id from jobs where status='queued' and run_after <= now()"
        "             order by id for update skip locked limit 1)"
        " returning *").fetchone()


def run_one(ctx: Context) -> dict | None:
    conn = ctx.conn
    job = claim(conn)
    if job is None:
        return None
    handler = HANDLERS.get(job["kind"])
    try:
        if handler is None:
            raise ValueError(f"unknown job kind {job['kind']!r}")
        commit = handler(ctx, job["payload"])
        with conn.transaction():
            result = commit(conn) if commit else {}
            conn.execute("update jobs set status='done', last_error=null where id=%s", (job["id"],))
        log.info("job %s %s -> %s", job["id"], job["kind"], result)
        return {"id": job["id"], "kind": job["kind"], "result": result}
    except Exception as e:  # noqa: BLE001 - every failure is recorded on the job
        final = job["attempts"] >= job["max_attempts"]
        conn.execute(
            "update jobs set status=%s, last_error=%s, locked_at=null,"
            " run_after = now() + make_interval(secs => %s) where id=%s",
            ("failed" if final else "queued", f"{type(e).__name__}: {e}", 2 ** job["attempts"] * 5, job["id"]))
        log.exception("job %s %s failed (attempt %s)", job["id"], job["kind"], job["attempts"])
        return {"id": job["id"], "kind": job["kind"], "error": str(e)}


def run_until_idle(ctx: Context, max_jobs: int = 10_000) -> list[dict]:
    """Drain ready jobs (used by tests and `db4ai maintain --once`)."""
    out = []
    for _ in range(max_jobs):
        r = run_one(ctx)
        if r is None:
            break
        out.append(r)
    return out


def schedule_periodic(conn: Conn) -> None:
    for kind in PERIODIC:
        store.enqueue(conn, kind, {}, dedupe_key=f"periodic:{kind}")


def run_forever(ctx: Context, poll_seconds: float = 1.0) -> None:
    last: dict[str, float] = {}
    while True:
        now = time.time()
        for kind, every in PERIODIC.items():
            if now - last.get(kind, 0) >= every:
                store.enqueue(ctx.conn, kind, {}, dedupe_key=f"periodic:{kind}")
                last[kind] = now
        if run_one(ctx) is None:
            time.sleep(poll_seconds)

