"""Slack/Teams notifications for pending harness decisions, delivered through the outbox (`jobs`).

* `propose` queues one `notify` job per configured webhook in the proposing transaction, so a notification
  exists iff the decision committed. Jobs are deduped per webhook and delayed a little, so a burst of
  proposals becomes one message.
* The job re-reads the decisions at send time: only those still pending, unexpired and not yet sent to that
  webhook are named. Sent rows are recorded in `notifications` after the POST succeeds (at-least-once).
* Messages hold no item content (no reason, diff, payload or evidence): a chat message can't be erased later.
  They name the action, team, confidence, proposer, expiry and a link to the harness page.
"""

from __future__ import annotations

import hashlib
from collections import Counter

import httpx

from . import store
from .config import settings
from .db import Conn

MAX_LISTED = 20  # decisions named per message; the rest are counted


def webhooks() -> list[tuple[str, str, str]]:
    """Configured webhooks as (id, format, url). `teams:` / `slack:` prefixes pick the format (default slack)."""
    out = []
    for raw in (settings().notify_webhooks or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        fmt, url = "slack", raw
        for prefix in ("slack:", "teams:"):
            if raw.startswith(prefix):
                fmt, url = prefix[:-1], raw[len(prefix):]
        out.append((hashlib.sha256(url.encode()).hexdigest()[:16], fmt, url))
    return out


def queue(conn: Conn) -> None:
    """Called in the transaction that created a pending decision."""
    delay = settings().notify_delay_seconds
    for wid, _, _ in webhooks():
        store.enqueue(conn, "notify", {"webhook": wid}, dedupe_key=f"notify:{wid}", delay_seconds=delay)


def unsent(conn: Conn, webhook: str) -> list[dict]:
    return conn.execute(
        "select d.id, d.action_type, d.confidence, d.expires_at, t.name as team, a.name as proposed_by"
        " from harness_decisions d left join teams t on t.id = d.team_id join actors a on a.id = d.proposed_by"
        " where d.status = 'pending' and (d.expires_at is null or d.expires_at > now())"
        " and not exists (select 1 from notifications n where n.decision_id = d.id and n.webhook = %s)"
        " order by d.proposed_at, d.id", (webhook,)).fetchall()


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
    hook = next((h for h in webhooks() if h[0] == webhook), None)
    if hook is None:  # webhook removed from config since the job was queued
        return []
    decisions = unsent(conn, webhook)
    if not decisions:
        return []
    title, lines = message(decisions, settings().public_url)
    post_json(hook[2], body(hook[1], title, lines))
    return decisions
