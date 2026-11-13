"""HTTP API + the minimal AIDBHarness web page (SPEC §5–6)."""

from __future__ import annotations

import contextlib
import html
import json
from typing import Iterator

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from . import service
from .auth import AccessDenied, Actor, actor_for_token
from .config import settings
from .db import Conn
from .harness import HarnessError

COOKIE = "db4ai_token"


class ItemIn(BaseModel):
    type: str
    title: str
    body: str = ""
    team: str | None = None
    metadata: dict = {}
    external_key: str | None = None
    source: str = "http"
    reason: str | None = None


class DecisionIn(BaseModel):
    approve: bool
    note: str | None = None
    edited_payload: dict | None = None


def create_app(database_url: str | None = None) -> FastAPI:
    pool = ConnectionPool(database_url or settings().database_url, open=False,
                          kwargs={"row_factory": dict_row, "autocommit": True})
    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        pool.open()
        try:
            yield
        finally:
            pool.close()

    app = FastAPI(title="DB4AI", version="1.0.0", lifespan=lifespan)

    def conn() -> Iterator[Conn]:
        with pool.connection() as c:
            yield c

    def actor(request: Request, c: Conn = Depends(conn)) -> Actor:
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else request.cookies.get(COOKIE)
        if not token:
            raise HTTPException(401, "missing token")
        try:
            return actor_for_token(c, token)
        except AccessDenied as e:
            raise HTTPException(401, str(e)) from e

    @app.exception_handler(AccessDenied)
    def _denied(request: Request, exc: AccessDenied):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(KeyError)
    def _missing(request: Request, exc: KeyError):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(HarnessError)
    def _harness(request: Request, exc: HarnessError):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": str(exc)}, status_code=409)

    # ------------------------------------------------------------ JSON API

    @app.post("/items")
    def post_item(body: ItemIn, a: Actor = Depends(actor), c: Conn = Depends(conn)):
        return service.save(c, a, **body.model_dump())

    @app.get("/items/{item_id}")
    def get_item(item_id: str, a: Actor = Depends(actor), c: Conn = Depends(conn)):
        return service.get(c, a, item_id)

    @app.get("/items/{item_id}/history")
    def get_history(item_id: str, a: Actor = Depends(actor), c: Conn = Depends(conn)):
        return service.history(c, a, item_id)

    @app.post("/items/{item_id}/revert/{version}")
    def post_revert(item_id: str, version: int, a: Actor = Depends(actor), c: Conn = Depends(conn)):
        return service.revert(c, a, item_id, version, source="http")

    @app.get("/search")
    def get_search(q: str, type: str | None = None, limit: int = 10, a: Actor = Depends(actor), c: Conn = Depends(conn)):
        return service.search(c, a, q, types=[type] if type else None, limit=limit)

    @app.get("/datasets/{name}")
    def get_dataset(name: str, team: str | None = None, limit: int = 100, a: Actor = Depends(actor), c: Conn = Depends(conn)):
        return service.dataset_rows(c, a, name, team, limit)

    @app.get("/harness/decisions")
    def get_decisions(status: str = "pending", a: Actor = Depends(actor), c: Conn = Depends(conn)):
        return service.pending(c, a, status)

    @app.post("/harness/decisions/{decision_id}")
    def post_decision(decision_id: str, body: DecisionIn, a: Actor = Depends(actor), c: Conn = Depends(conn)):
        return service.decide(c, a, decision_id, body.approve, body.note, body.edited_payload)

    @app.post("/harness/decisions/{decision_id}/revert")
    def post_decision_revert(decision_id: str, a: Actor = Depends(actor), c: Conn = Depends(conn)):
        return service.revert_decision(c, a, decision_id)

    @app.get("/healthz")
    def healthz(c: Conn = Depends(conn)):
        c.execute("select 1")
        return {"ok": True}

    # ------------------------------------------------------------ harness web page

    @app.get("/", response_class=HTMLResponse)
    def index():
        return RedirectResponse("/harness")

    @app.get("/login", response_class=HTMLResponse)
    def login_page():
        return _page("Sign in", """
          <form method="post" action="/login" class="card">
            <label>API token <input name="token" type="password" autocomplete="off" required></label>
            <button>Sign in</button>
          </form>""")

    @app.post("/login")
    def login(token: str = Form(...), c: Conn = Depends(conn)):
        try:
            actor_for_token(c, token)
        except AccessDenied:
            return HTMLResponse(_page("Sign in", "<p class=err>Invalid token.</p>"), status_code=401)
        r = RedirectResponse("/harness", status_code=303)
        r.set_cookie(COOKIE, token, httponly=True, samesite="strict")
        return r

    @app.get("/harness", response_class=HTMLResponse)
    def harness_page(request: Request, status: str = "pending", c: Conn = Depends(conn)):
        token = request.cookies.get(COOKIE)
        if not token:
            return RedirectResponse("/login")
        try:
            a = actor_for_token(c, token)
        except AccessDenied:
            return RedirectResponse("/login")
        rows = service.pending(c, a, status)
        tabs = " ".join(
            f'<a class="tab{" on" if s == status else ""}" href="/harness?status={s}">{s.replace("_", " ")}</a>'
            for s in ("pending", "applied_auto", "approved", "rejected", "reverted", "expired"))
        cards = "".join(_decision_card(d, status) for d in rows) or "<p class=muted>Nothing here.</p>"
        return _page(f"AIDBHarness — {html.escape(a.name)}", f"<nav>{tabs}</nav>{cards}")

    @app.post("/harness/{decision_id}/form")
    def harness_form(decision_id: str, request: Request, verb: str = Form(...), note: str = Form(""),
                     edited: str = Form(""), c: Conn = Depends(conn)):
        a = actor_for_token(c, request.cookies.get(COOKIE, ""))
        if verb == "revert":
            service.revert_decision(c, a, decision_id, note or None)
        else:
            payload = json.loads(edited) if edited.strip() else None
            service.decide(c, a, decision_id, verb == "approve", note or None, payload)
        return RedirectResponse("/harness", status_code=303)

    return app


def _decision_card(d: dict, status: str) -> str:
    e = html.escape
    conf = f"{d['confidence']:.2f}" if d.get("confidence") is not None else "—"
    actions = ""
    if status == "pending":
        actions = f"""
        <form method="post" action="/harness/{d['id']}/form">
          <details><summary>Edit payload before approving</summary>
            <textarea name="edited" rows="6">{e(json.dumps(d['payload'], indent=2))}</textarea></details>
          <input name="note" placeholder="Note (optional)">
          <button name="verb" value="approve">Approve</button>
          <button name="verb" value="reject" class="secondary">Reject</button>
        </form>"""
    elif status in ("applied_auto", "approved"):
        until = f" until {e(str(d['revert_until'])[:16])}" if d.get("revert_until") else ""
        actions = f"""<form method="post" action="/harness/{d['id']}/form">
          <input name="note" placeholder="Why revert?"><button name="verb" value="revert" class="secondary">Revert{until}</button></form>"""
    return f"""
    <article class="card">
      <header><b>{e(d['action_type'])}</b> <span class="pill {e(d['tier'])}">{e(d['tier'])}</span>
        <span class="muted">team {e(str(d.get('team') or '—'))} · confidence {conf} · by {e(d['proposed_by'])}</span></header>
      <p>{e(d.get('reason') or '')}</p>
      <pre>{e(json.dumps(d['diff'], indent=2))}</pre>
      {f"<details><summary>Evidence</summary><pre>{e(json.dumps(d['evidence'], indent=2))}</pre></details>" if d.get('evidence') else ""}
      {actions}
    </article>"""


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>
:root {{ --bg:#f7f7f5; --fg:#1d1d1b; --muted:#6b6b66; --card:#fff; --line:#e3e3df; --accent:#2f5bd3; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#151514; --fg:#ececea; --muted:#9a9a94; --card:#1f1f1d; --line:#2e2e2b; --accent:#7d9cff; }} }}
body {{ background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, sans-serif; max-width:860px; margin:0 auto; padding:24px 16px; }}
h1 {{ font-size:20px; }} .muted {{ color:var(--muted); font-size:13px; }}
nav {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:16px; }}
.tab {{ padding:4px 10px; border:1px solid var(--line); border-radius:99px; color:var(--fg); text-decoration:none; font-size:13px; }}
.tab.on {{ background:var(--accent); color:#fff; border-color:var(--accent); }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; margin-bottom:12px; }}
pre {{ background:var(--bg); padding:10px; border-radius:6px; overflow:auto; font-size:12px; }}
.pill {{ font-size:11px; padding:1px 8px; border-radius:99px; border:1px solid var(--line); }}
.pill.ask_first {{ border-color:#d97706; color:#d97706; }} .pill.auto_notice {{ border-color:var(--accent); color:var(--accent); }}
input, textarea {{ width:100%; box-sizing:border-box; margin:6px 0; padding:6px; background:var(--bg); color:var(--fg); border:1px solid var(--line); border-radius:6px; }}
button {{ background:var(--accent); color:#fff; border:0; border-radius:6px; padding:6px 14px; margin-right:6px; cursor:pointer; }}
button.secondary {{ background:transparent; color:var(--fg); border:1px solid var(--line); }} .err {{ color:#c62828; }}
</style></head><body><h1>{html.escape(title)}</h1>{body}</body></html>"""
