"""Slack/Teams notifications for pending harness decisions, delivered through the outbox (`jobs`).

* `propose` queues one `notify` job per webhook that should hear about the decision, in the proposing
  transaction, so a notification exists iff the decision committed. Jobs are deduped per webhook and delayed
  a little, so a burst of proposals becomes one message.
* Global webhooks (`DB4AI_NOTIFY_WEBHOOKS`) hear about every decision. A team route (`notify_routes`: team →
  name of an env var `DB4AI_WEBHOOK_*` holding the URL) hears about that team's decisions, except admin-only
  actions (erasure/redaction), which only admins can decide. The same URL in several places is one webhook.
* The job re-reads the decisions at send time: only those still pending, unexpired and not yet sent to that
  webhook are named. Sent rows are recorded in `notifications` after the POST succeeds (at-least-once).
* Messages hold no item content (no reason, diff, payload or evidence): a chat message can't be erased later.
  They name the action, team, confidence, proposer, expiry and a link to the harness page.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from uuid import UUID

import httpx

from . import store
from .auth import AccessDenied, Actor
from .config import settings
from .db import Conn, Provenance, log_change

log = logging.getLogger(__name__)

MAX_LISTED = 20  # decisions named per message; the rest are counted
ENV_NAME = re.compile(r"^DB4AI_WEBHOOK_[A-Z0-9_]{1,64}$")  # also a check constraint on notify_routes


@dataclass
class Webhook:
    id: str
    fmt: str
    url: str
    all_teams: bool = False                            # a global webhook: every decision
    teams: set[UUID] = field(default_factory=set)      # team routes: that team's non-admin-only decisions


def _parse(value: str | None) -> list[tuple[str, str, str]]:
    """Webhook URLs as (id, format, url). `teams:` / `slack:` prefixes pick the format (default slack)."""
    out = []
    for raw in (value or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        fmt, url = "slack", raw
        for prefix in ("slack:", "teams:"):
            if raw.startswith(prefix):
                fmt, url = prefix[:-1], raw[len(prefix):]
        out.append((hashlib.sha256(url.encode()).hexdigest()[:16], fmt, url))
    return out


def webhooks(conn: Conn) -> dict[str, Webhook]:
    """Every configured webhook by id, with the decisions it should hear about. Read from env + `notify_routes`."""
    out: dict[str, Webhook] = {}
    for wid, fmt, url in _parse(settings().notify_webhooks):
        out.setdefault(wid, Webhook(wid, fmt, url)).all_teams = True
    for r in conn.execute("select team_id, env_name from notify_routes order by env_name").fetchall():
        hooks = _parse(os.environ.get(r["env_name"]))
        if not hooks:
            log.warning("notify route for team %s: %s is not set", r["team_id"], r["env_name"])
        for wid, fmt, url in hooks:
            out.setdefault(wid, Webhook(wid, fmt, url)).teams.add(r["team_id"])
    return out


def _admin_only() -> frozenset[str]:
    from .harness import ADMIN_ONLY  # harness imports this module
    return ADMIN_ONLY


def queue(conn: Conn, team_id: UUID | None = None, action_type: str | None = None) -> None:
    """Called in the transaction that created a pending decision of `action_type` for `team_id`."""
    delay = settings().notify_delay_seconds
    routed = team_id is not None and action_type not in _admin_only()
    for wid, hook in webhooks(conn).items():
        if hook.all_teams or (routed and team_id in hook.teams):
            store.enqueue(conn, "notify", {"webhook": wid}, dedupe_key=f"notify:{wid}", delay_seconds=delay)


def unsent(conn: Conn, hook: Webhook) -> list[dict]:
    return conn.execute(
        "select d.id, d.action_type, d.confidence, d.expires_at, t.name as team, a.name as proposed_by"
        " from harness_decisions d left join teams t on t.id = d.team_id join actors a on a.id = d.proposed_by"
        " where d.status = 'pending' and (d.expires_at is null or d.expires_at > now())"
        " and (%s or (d.team_id = any(%s) and d.action_type <> all(%s)))"
        " and not exists (select 1 from notifications n where n.decision_id = d.id and n.webhook = %s)"
        " order by d.proposed_at, d.id",
        (hook.all_teams, list(hook.teams), list(_admin_only()), hook.id)).fetchall()


# ------------------------------------------------------------------ routes

def _require_manage(actor: Actor, team_id: UUID) -> None:
    if not (actor.is_admin or actor.can_decide(team_id)):
        raise AccessDenied("only admins or team owners can change notification routes")


def add_route(conn: Conn, actor: Actor, team_id: UUID, env_name: str) -> dict:
    """Route `team_id`'s pending decisions to the webhook in env var `env_name` (the URL itself stays in env)."""
    _require_manage(actor, team_id)
    if not ENV_NAME.match(env_name):
        raise ValueError("env_name must look like DB4AI_WEBHOOK_<NAME> (A-Z, 0-9, _)")
    with conn.transaction():
        row = conn.execute("insert into notify_routes (team_id, env_name, created_by) values (%s,%s,%s)"
                           " on conflict do nothing returning team_id", (team_id, env_name, actor.id)).fetchone()
        if row is not None:
            log_change(conn, Provenance(actor.id, "notify_route"), "notify_route", str(team_id), "add",
                       after={"env_name": env_name})
    return route(team_id, env_name)


def remove_route(conn: Conn, actor: Actor, team_id: UUID, env_name: str) -> bool:
    _require_manage(actor, team_id)
    with conn.transaction():
        row = conn.execute("delete from notify_routes where team_id=%s and env_name=%s returning team_id",
                           (team_id, env_name)).fetchone()
        if row is not None:
            log_change(conn, Provenance(actor.id, "notify_route"), "notify_route", str(team_id), "remove",
                       before={"env_name": env_name})
    return row is not None


def route(team_id: UUID, env_name: str) -> dict:
    """A route as shown to people: whether the variable is set, never its value."""
    return {"team_id": team_id, "env_name": env_name, "configured": bool(_parse(os.environ.get(env_name)))}


def routes(conn: Conn, actor: Actor, team_id: UUID) -> list[dict]:
    actor.require_read(team_id)
    rows = conn.execute("select env_name from notify_routes where team_id=%s order by env_name", (team_id,)).fetchall()
    return [route(team_id, r["env_name"]) for r in rows]


def _line(d: dict) -> str:
    conf = f", confidence {d['confidence']:.2f}" if d["confidence"] is not None else ""
    exp = f", expires {d['expires_at']:%Y-%m-%d}" if d["expires_at"] is not None else ""
    return f"• {d['action_type']} (team {d['team'] or 'global'}{conf}) proposed by {d['proposed_by']}{exp} [{d['id']}]"


def message(decisions: list[dict], base_url: str | None) -> tuple[str, list[str]]:
    n = len(decisions)
    counts = Counter(d["action_type"] for d in decisions)
    summary = ", ".join(f"{c} × {a}" for a, c in sorted(counts.items()))
    title = f"DB4AI: {n} harness decision{'s' if n != 1 else ''} waiting for review ({summary})"
    lines = [_line(d) for d in decisions[:MAX_LISTED]]
    if n > MAX_LISTED:
        lines.append(f"…and {n - MAX_LISTED} more")
    lines.append(f"Review: {base_url.rstrip('/')}/harness" if base_url else "Review: /harness or harness_list_pending")
    return title, lines


def body(fmt: str, title: str, lines: list[str]) -> dict:
    if fmt == "teams":  # Teams Workflows webhook: an Adaptive Card attachment
        return {"type": "message", "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
                        "version": "1.4", "body": [{"type": "TextBlock", "text": title, "weight": "Bolder", "wrap": True}]
                        + [{"type": "TextBlock", "text": ln, "wrap": True} for ln in lines]}}]}
    return {"text": "\n".join([f"*{title}*", *lines])}


def post_json(url: str, payload: dict) -> None:
    """POST a webhook message; any non-2xx raises, so the worker retries with backoff."""
    r = httpx.post(url, json=payload, timeout=10.0)
    r.raise_for_status()


def send(conn: Conn, webhook: str) -> list[dict]:
    """Deliver unsent pending decisions to one webhook; returns the decisions named (the caller records them)."""
    hook = webhooks(conn).get(webhook)
    if hook is None:  # webhook removed from config since the job was queued
        return []
    decisions = unsent(conn, hook)
    if not decisions:
        return []
    title, lines = message(decisions, settings().public_url)
    post_json(hook.url, body(hook.fmt, title, lines))
    return decisions
