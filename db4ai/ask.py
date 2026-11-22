"""`ask`: answer a question from DB4AI items, citing the item ids and versions it used.

Read-only: it retrieves with `store.search` (so team access, active-only and chunking all apply), hands the
passages to the LLM and keeps only citations that point at a passage it was given. An answer without at least
one quote copied verbatim from a passage is not returned (fail closed): `answered` is false and `answer` says
nothing was found.
"""

from __future__ import annotations

import re

from . import store
from .auth import Actor
from .config import settings
from .db import Conn
from .embeddings import chunk_spans, tokens

NOT_FOUND = "I couldn't find an answer to that in DB4AI."
MAX_QUESTION = 2000


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


def ask(conn: Conn, actor: Actor, question: str, *, llm, embedder, limit: int = 8,
        types: list[str] | None = None) -> dict:
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
