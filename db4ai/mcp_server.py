"""MCP server: how Claude reads, writes and maintains DB4AI (SPEC §5–6).

Run with `db4ai mcp` (stdio). Identity comes from DB4AI_TOKEN, so every write Claude makes is
attributed to that actor and stamped with source `claude-mcp`.
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import service
from .auth import actor_for_token
from .db import connect

SOURCE = "claude-mcp"

mcp = MCPServer(
    name="db4ai",
    instructions=(
        "DB4AI is the company's self-maintaining database for meetings, documents, team memory, decisions "
        "and analytics datasets. Use `ask` to answer questions from it (cite the item ids it returns) and search before saving to avoid duplicates. Every write is versioned and "
        "attributed; nothing is overwritten. Pending AIDBHarness decisions need a human: show the diff and "
        "evidence to the user and only call harness_decide with their explicit answer."
    ),
)

_conn = None


def _ctx():
    global _conn
    if _conn is None or _conn.closed:
        _conn = connect()
    token = os.environ.get("DB4AI_TOKEN")
    if not token:
        raise RuntimeError("DB4AI_TOKEN is not set")
    return _conn, actor_for_token(_conn, token)


@mcp.tool()
def save_meeting(title: str, notes: str, attendees: list[str] | None = None, date: str | None = None,
                 decisions: list[str] | None = None, action_items: list[str] | None = None,
                 team: str | None = None) -> dict[str, Any]:
    """Save meeting notes or a transcript summary as a versioned `meeting` item."""
    c, a = _ctx()
    meta = {k: v for k, v in {"attendees": attendees, "date": date, "decisions": decisions,
                              "action_items": action_items}.items() if v}
    return service.save(c, a, type="meeting", title=title, body=notes, team=team, metadata=meta, source=SOURCE)


@mcp.tool()
def save_document(title: str, body: str, team: str | None = None, metadata: dict | None = None,
                  key: str | None = None) -> dict[str, Any]:
    """Save a document. Pass a stable `key` (e.g. its URL or path) so later saves add versions instead of copies."""
    c, a = _ctx()
    return service.save(c, a, type="document", title=title, body=body, team=team, metadata=metadata,
                        external_key=key, source=SOURCE)


@mcp.tool()
def remember(fact: str, title: str | None = None, team: str | None = None, key: str | None = None) -> dict[str, Any]:
    """Store a durable piece of team memory (a fact, preference, convention or context)."""
    c, a = _ctx()
    return service.save(c, a, type="memory", title=title or fact[:80], body=fact, team=team,
                        external_key=key, source=SOURCE)


@mcp.tool()
def log_decision(decision: str, rationale: str = "", owner: str | None = None, team: str | None = None) -> dict[str, Any]:
    """Record a decision the team made, with its rationale and owner."""
    c, a = _ctx()
    meta = {"owner": owner} if owner else {}
    return service.save(c, a, type="decision", title=decision[:120], body=f"{decision}\n\n{rationale}".strip(),
                        team=team, metadata=meta, source=SOURCE)


@mcp.tool()
def search(query: str, type: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """Hybrid full-text + semantic search over items you can access. `type`: meeting|document|memory|decision."""
    c, a = _ctx()
    return service.search(c, a, query, types=[type] if type else None, limit=limit)


@mcp.tool()
def ask(question: str, type: str | None = None, limit: int = 8,
        history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Answer a question from DB4AI items you can access, citing the item ids and versions used.

    Returns `answered` (false if nothing relevant was found), `answer`, `citations` (item_id, version, title,
    quote) and the `sources` searched. Changes nothing (an audit row is written if DB4AI_ASK_LOG is on).
    For a follow-up, pass `history`: the earlier turns, oldest first, each {question, answer, citations} as
    returned by `ask` (at most 10). They help resolve "it"/"that"; only DB4AI items are ever cited.
    """
    c, a = _ctx()
    return service.ask(c, a, question, types=[type] if type else None, limit=limit, history=history)


@mcp.tool()
def get_item(item_id: str) -> dict[str, Any]:
    """Current version of an item, with its AI metadata and links."""
    c, a = _ctx()
    return service.get(c, a, item_id)


@mcp.tool()
def history(item_id: str) -> list[dict[str, Any]]:
    """Every version of an item: who (human or agent) changed it, from where, and why."""
    c, a = _ctx()
    return service.history(c, a, item_id)


@mcp.tool()
def revert(item_id: str, to_version: int, reason: str) -> dict[str, Any]:
    """Restore an earlier version (recorded as a new version; nothing is lost)."""
    c, a = _ctx()
    return service.revert(c, a, item_id, to_version, reason, source=SOURCE)


@mcp.tool()
def request_erasure(item_id: str, reason: str) -> dict[str, Any]:
    """Ask an admin to erase an item's content from every version (e.g. a GDPR or legal request).

    Nothing changes until an admin approves it in the AIDBHarness; a tombstone records who asked and why.
    """
    c, a = _ctx()
    return service.request_erasure(c, a, item_id, reason, source=SOURCE)


@mcp.tool()
def request_redaction(item_id: str, terms: list[str], reason: str) -> dict[str, Any]:
    """Ask an admin to replace `terms` (case-insensitive) with [redacted] in every version of an item."""
    c, a = _ctx()
    return service.request_erasure(c, a, item_id, reason, terms, source=SOURCE)


@mcp.tool()
def dataset(name: str, team: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Read rows from a structured dataset (also queryable by BI tools as data.<table>)."""
    c, a = _ctx()
    return service.dataset_rows(c, a, name, team, limit)


@mcp.tool()
def harness_list_pending(status: str = "pending") -> list[dict[str, Any]]:
    """AIDBHarness: decisions waiting for a human (or, with status=applied_auto, recent auto changes)."""
    c, a = _ctx()
    return service.pending(c, a, status)


@mcp.tool()
def harness_decide(decision_id: str, approve: bool, note: str | None = None,
                   edited_payload: dict | None = None) -> dict[str, Any]:
    """AIDBHarness: approve or reject a pending decision. Only call with the user's explicit answer."""
    c, a = _ctx()
    return service.decide(c, a, decision_id, approve, note, edited_payload)


@mcp.tool()
def harness_revert(decision_id: str, note: str | None = None) -> dict[str, Any]:
    """AIDBHarness: undo an automatically applied change (within its revert window)."""
    c, a = _ctx()
    return service.revert_decision(c, a, decision_id, note)


def main() -> None:
    mcp.run("stdio")
