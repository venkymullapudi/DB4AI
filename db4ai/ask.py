"""`ask`: answer a question from DB4AI items, citing the item ids and versions it used.

It retrieves with `store.search` (so team access, active-only and chunking all apply), hands the
passages to the LLM and keeps only citations that point at a passage it was given. An answer without at least
one quote copied verbatim from a passage is not returned (fail closed): `answered` is false and `answer` says
nothing was found.

Nothing about the items changes. With `DB4AI_ASK_LOG` set to `ids` or `text`, each call writes one `ask_log` row
(who asked, the question's hash or text, which item versions were searched and cited) in the same transaction as
the answer; if that write fails, no answer is returned.
"""

from __future__ import annotations

import hashlib
import re
from uuid import UUID

from psycopg.types.json import Jsonb

from . import store
from .auth import Actor
from .config import settings
from .db import Conn, transaction
from .embeddings import chunk_spans, tokens

NOT_FOUND = "I couldn't find an answer to that in DB4AI."
MAX_QUESTION = 2000
LOG_MODES = ("off", "ids", "text")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _best_window(body: str, question: str) -> str:
    """For items found only by full-text search: the chunk sharing the most terms with the question."""
    terms = set(tokens(question))
    spans = chunk_spans(body, settings().chunk_chars)
    a, b = max(spans, key=lambda s: len(terms & set(tokens(body[s[0]:s[1]]))))
    return body[a:b]


def passages(conn: Conn, actor: Actor, question: str, *, embedder, limit: int = 8,
             types: list[str] | None = None) -> list[dict]:
    hits = store.search(conn, actor, question, embedder=embedder, types=types, limit=limit)
    missing = [h["id"] for h in hits if h.get("chunk") is None]
    bodies = {}
    if missing:
        rows = conn.execute("select id, body from items_current where id = any(%s)", (missing,)).fetchall()
        bodies = {r["id"]: r["body"] for r in rows}
    out = []
    for h in hits:
        text = h["chunk"]["text"] if h.get("chunk") else _best_window(bodies.get(h["id"], ""), question)
        text = text.strip() or h["title"]
        out.append({"item_id": h["id"], "version": h["version"], "title": h["title"], "type": h["type"],
                    "score": h["score"], "text": text})
    return out


def log_mode() -> str:
    mode = (settings().ask_log or "off").strip().lower()
    if mode not in LOG_MODES:
        raise ValueError(f"DB4AI_ASK_LOG must be one of {', '.join(LOG_MODES)}, not {mode!r}")
    return mode


def question_hash(question: str) -> str:
    return hashlib.sha256(_norm(question).encode()).hexdigest()


def ask(conn: Conn, actor: Actor, question: str, *, llm, embedder, limit: int = 8,
        types: list[str] | None = None) -> dict:
    mode = log_mode()  # checked first: a bad setting answers nothing
    if mode == "off":
        return _ask(conn, actor, question, llm=llm, embedder=embedder, limit=limit, types=types)
    with transaction(conn):
        result = _ask(conn, actor, question, llm=llm, embedder=embedder, limit=limit, types=types)
        _log(conn, actor, result, types, mode)
    return result


def _pairs(rows: list[dict]) -> list[dict]:
    out, seen = [], set()
    for r in rows:
        k = (str(r["item_id"]), r["version"])
        if k not in seen:
            seen.add(k)
            out.append({"item_id": k[0], "version": k[1]})
    return out


def _log(conn: Conn, actor: Actor, result: dict, types: list[str] | None, mode: str) -> None:
    sources, cited = _pairs(result["sources"]), _pairs(result["citations"])
    conn.execute(
        "insert into ask_log (actor_id, question_sha256, question, types, answered, model, sources, citations,"
        " source_items, cited_items) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (actor.id, question_hash(result["question"]), result["question"] if mode == "text" else None, types,
         result["answered"], result["model"], Jsonb(sources), Jsonb(cited),
         list(dict.fromkeys(UUID(p["item_id"]) for p in sources)),
         list(dict.fromkeys(UUID(p["item_id"]) for p in cited))))


def log(conn: Conn, actor: Actor, *, actor_name: str | None = None, item_id: UUID | None = None,
        limit: int = 50) -> list[dict]:
    """Audit rows, newest first. Admins see every row; anyone else only their own."""
    where, args = [], []
    if not actor.is_admin:
        where.append("l.actor_id = %s")
        args.append(actor.id)
    if actor_name:
        where.append("a.name = %s")
        args.append(actor_name)
    if item_id:
        where.append("%s = any(l.source_items)")
        args.append(item_id)
    sql = ("select l.id, a.name as actor, l.asked_at, l.question_sha256, l.question, l.types, l.answered, l.model,"
           " l.sources, l.citations from ask_log l join actors a on a.id = l.actor_id"
           + (" where " + " and ".join(where) if where else "") + " order by l.asked_at desc, l.id limit %s")
    return conn.execute(sql, (*args, max(1, min(limit, 500)))).fetchall()


def _ask(conn: Conn, actor: Actor, question: str, *, llm, embedder, limit: int,
         types: list[str] | None) -> dict:
    question = question.strip()
    if not question:
        raise ValueError("question is required")
    if len(question) > MAX_QUESTION:
        raise ValueError(f"question is longer than {MAX_QUESTION} characters")
    limit = max(1, min(limit, 20))
    ps = passages(conn, actor, question, embedder=embedder, limit=limit, types=types)
    result = {"question": question, "answered": False, "answer": NOT_FOUND, "citations": [],
              "sources": [{k: p[k] for k in ("item_id", "version", "title", "type", "score")} for p in ps],
              "model": llm.name}
    if not ps:
        return result
    texts = [f"{p['title']}\n{p['text']}" for p in ps]
    reply = llm.answer(question, texts)
    citations, seen = [], set()
    for c in reply.citations:
        if not 1 <= c.passage <= len(ps):
            continue  # invented passage number
        p = ps[c.passage - 1]
        quote = c.quote.strip()
        verbatim = bool(quote) and _norm(quote) in _norm(texts[c.passage - 1])
        key = (p["item_id"], quote if verbatim else None)
        if key in seen:
            continue
        seen.add(key)
        citations.append({"item_id": p["item_id"], "version": p["version"], "title": p["title"],
                          "type": p["type"], "quote": quote if verbatim else None})
    # Grounded = the model says it answered and at least one quote is copied verbatim from a passage.
    if reply.answered and reply.answer.strip() and any(c["quote"] for c in citations):
        result.update(answered=True, answer=reply.answer.strip(), citations=citations)
    return result
