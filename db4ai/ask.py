"""`ask`: answer a question from DB4AI items, citing the item ids and versions it used.

It retrieves with `store.search` (so team access, active-only and chunking all apply), hands the
passages to the LLM and keeps only citations that point at a passage it was given. An answer without at least
one quote copied verbatim from a passage is not returned (fail closed): `answered` is false and `answer` says
nothing was found.

Follow-ups: the caller may pass `history`, the earlier turns of the conversation (question, answer, cited item ids),
oldest first. History is context, never evidence: it widens retrieval (the previous question, and the items cited
earlier re-read under the actor's access) and is shown to the model, but only passages DB4AI retrieved can be cited.

As of a time: with `as_of`, retrieval is `store.search_as_of` (full-text over each item's version at or before then)
and passages, carried items and citations all use those old versions, so the answer reads the items as they were.

Nothing about the items changes. With `DB4AI_ASK_LOG` set to `ids` or `text`, each call writes one `ask_log` row
(who asked, the question's hash or text, which item versions were searched and cited) in the same transaction as
the answer; if that write fails, no answer is returned.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
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
MAX_TURNS = 10
MAX_ANSWER = 4000
MAX_TURN_CITATIONS = 20
MAX_CARRIED = 5


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _best_window(body: str, question: str) -> str:
    """For items found only by full-text search: the chunk sharing the most terms with the question."""
    terms = set(tokens(question))
    spans = chunk_spans(body, settings().chunk_chars)
    a, b = max(spans, key=lambda s: len(terms & set(tokens(body[s[0]:s[1]]))))
    return body[a:b]


def _turns(history: list[dict] | None) -> list[dict]:
    """Validate client-supplied earlier turns. Bad input is a ValueError (400), never silently trimmed."""
    if not history:
        return []
    if not isinstance(history, list):
        raise ValueError("history must be a list of turns")
    if len(history) > MAX_TURNS:
        raise ValueError(f"history has more than {MAX_TURNS} turns")
    out = []
    for t in history:
        if not isinstance(t, dict):
            raise ValueError("each history turn must be an object")
        q = str(t.get("question") or "").strip()
        a = str(t.get("answer") or "").strip()
        if not q:
            raise ValueError("each history turn needs a question")
        if len(q) > MAX_QUESTION:
            raise ValueError(f"a history question is longer than {MAX_QUESTION} characters")
        if len(a) > MAX_ANSWER:
            raise ValueError(f"a history answer is longer than {MAX_ANSWER} characters")
        cites = t.get("citations") or []
        if not isinstance(cites, list) or len(cites) > MAX_TURN_CITATIONS:
            raise ValueError(f"each history turn may cite at most {MAX_TURN_CITATIONS} items")
        ids = []
        for c in cites:
            raw = c.get("item_id") if isinstance(c, dict) else c
            try:
                ids.append(UUID(str(raw)))
            except ValueError:
                raise ValueError(f"history citation item_id {raw!r} is not a uuid") from None
        out.append({"question": q, "answer": a, "item_ids": ids})
    return out


def _carried_ids(turns: list[dict]) -> list[UUID]:
    """Items cited earlier, newest turn first, at most MAX_CARRIED."""
    out: list[UUID] = []
    for t in reversed(turns):
        for i in t["item_ids"]:
            if i not in out:
                out.append(i)
    return out[:MAX_CARRIED]


def _hit_passage(h: dict, text: str) -> dict:
    return {"item_id": h["id"], "version": h["version"], "title": h["title"], "type": h["type"],
            "score": h["score"], "text": text.strip() or h["title"]}


def _search(conn: Conn, actor: Actor, q: str, *, embedder, limit: int, types: list[str] | None,
            as_of: datetime | None) -> list[dict]:
    if as_of is not None:
        return store.search_as_of(conn, actor, q, as_of, types=types, limit=limit)
    return store.search(conn, actor, q, embedder=embedder, types=types, limit=limit)


def _carried_rows(conn: Conn, actor: Actor, ids: list[UUID], types: list[str] | None,
                  as_of: datetime | None) -> dict:
    """Earlier-cited items re-read under search's rules: readable (team + ACL, or shared), active, the type filter. As of a time:
    the version then must be active, and items erased since are dropped (as in `store.search_as_of`)."""
    teams = None if actor.is_admin else list(actor.teams)
    vw = store.viewer(actor)
    if as_of is None:
        rows = conn.execute(
            "select id, version, title, type, body from items_current where id = any(%(ids)s) and status = 'active'"
            " and (%(types)s::text[] is null or type = any(%(types)s::text[]))"
            " and " + store.readable("team_id", "id"),
            {"ids": ids, "teams": teams, "types": types, "viewer": vw}).fetchall()
    else:
        rows = conn.execute(
            "select distinct on (v.item_id) v.item_id as id, v.version, v.title, i.type, v.body, v.status"
            " from item_versions v join items i on i.id = v.item_id join items_current c on c.id = i.id"
            " where v.item_id = any(%(ids)s) and v.created_at <= %(t)s and c.status <> 'erased'"
            " and (%(types)s::text[] is null or i.type = any(%(types)s::text[]))"
            " and " + store.readable("i.team_id", "i.id") +
            " order by v.item_id, v.version desc",
            {"ids": ids, "t": as_of, "teams": teams, "types": types, "viewer": vw}).fetchall()
        rows = [r for r in rows if r["status"] == "active"]
    return {r["id"]: r for r in rows}


def passages(conn: Conn, actor: Actor, question: str, *, embedder, limit: int = 8,
             types: list[str] | None = None, turns: list[dict] | None = None,
             as_of: datetime | None = None) -> list[dict]:
    turns = turns or []
    hits = _search(conn, actor, question, embedder=embedder, types=types, limit=limit, as_of=as_of)
    focus = question
    if turns:
        focus = f"{turns[-1]['question']} {question}"
        if len(hits) < limit:  # the follow-up alone may be too vague: search it with the previous question
            seen = {h["id"] for h in hits}
            more = _search(conn, actor, focus, embedder=embedder, types=types, limit=limit, as_of=as_of)
            hits += [h for h in more if h["id"] not in seen][:limit - len(hits)]
    missing = [h for h in hits if h.get("chunk") is None]
    bodies = {}
    if missing and as_of is None:
        rows = conn.execute("select id, body from items_current where id = any(%s)",
                            ([h["id"] for h in missing],)).fetchall()
        bodies = {r["id"]: r["body"] for r in rows}
    elif missing:  # the body of the version that matched, not the current one
        rows = conn.execute(
            "select v.item_id as id, v.body from item_versions v"
            " join unnest(%s::uuid[], %s::int[]) as m(item_id, version) using (item_id, version)",
            ([h["id"] for h in missing], [h["version"] for h in missing])).fetchall()
        bodies = {r["id"]: r["body"] for r in rows}
    out = [_hit_passage(h, h["chunk"]["text"] if h.get("chunk") else _best_window(bodies.get(h["id"], ""), focus))
           for h in hits]
    carried = [i for i in _carried_ids(turns) if i not in {p["item_id"] for p in out}]
    if carried:
        by_id = _carried_rows(conn, actor, carried, types, as_of)
        for i in carried:
            if i in by_id:
                r = by_id[i]
                out.append(_hit_passage({**r, "score": 0.0}, _best_window(r["body"], focus)))
    return out


def log_mode() -> str:
    mode = (settings().ask_log or "off").strip().lower()
    if mode not in LOG_MODES:
        raise ValueError(f"DB4AI_ASK_LOG must be one of {', '.join(LOG_MODES)}, not {mode!r}")
    return mode


def question_hash(question: str) -> str:
    return hashlib.sha256(_norm(question).encode()).hexdigest()


def ask(conn: Conn, actor: Actor, question: str, *, llm, embedder, limit: int = 8,
        types: list[str] | None = None, history: list[dict] | None = None, as_of: datetime | None = None) -> dict:
    mode = log_mode()  # checked first: a bad setting answers nothing
    turns = _turns(history)
    kw = dict(llm=llm, embedder=embedder, limit=limit, types=types, turns=turns, as_of=as_of)
    if mode == "off":
        return _ask(conn, actor, question, **kw)
    with transaction(conn):
        result = _ask(conn, actor, question, **kw)
        _log(conn, actor, result, types, mode, len(turns), as_of)
    return result


def _pairs(rows: list[dict]) -> list[dict]:
    out, seen = [], set()
    for r in rows:
        k = (str(r["item_id"]), r["version"])
        if k not in seen:
            seen.add(k)
            out.append({"item_id": k[0], "version": k[1]})
    return out


def _log(conn: Conn, actor: Actor, result: dict, types: list[str] | None, mode: str, history_turns: int,
         as_of: datetime | None) -> None:
    sources, cited = _pairs(result["sources"]), _pairs(result["citations"])
    conn.execute(
        "insert into ask_log (actor_id, question_sha256, question, types, answered, model, sources, citations,"
        " source_items, cited_items, history_turns, as_of) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (actor.id, question_hash(result["question"]), result["question"] if mode == "text" else None, types,
         result["answered"], result["model"], Jsonb(sources), Jsonb(cited),
         list(dict.fromkeys(UUID(p["item_id"]) for p in sources)),
         list(dict.fromkeys(UUID(p["item_id"]) for p in cited)), history_turns, as_of))


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
           " l.sources, l.citations, l.history_turns, l.as_of from ask_log l join actors a on a.id = l.actor_id"
           + (" where " + " and ".join(where) if where else "") + " order by l.asked_at desc, l.id limit %s")
    return conn.execute(sql, (*args, max(1, min(limit, 500)))).fetchall()


def _ask(conn: Conn, actor: Actor, question: str, *, llm, embedder, limit: int,
         types: list[str] | None, turns: list[dict], as_of: datetime | None) -> dict:
    question = question.strip()
    if not question:
        raise ValueError("question is required")
    if len(question) > MAX_QUESTION:
        raise ValueError(f"question is longer than {MAX_QUESTION} characters")
    limit = max(1, min(limit, 20))
    ps = passages(conn, actor, question, embedder=embedder, limit=limit, types=types, turns=turns, as_of=as_of)
    result = {"question": question, "answered": False, "answer": NOT_FOUND, "citations": [],
              "sources": [{k: p[k] for k in ("item_id", "version", "title", "type", "score")} for p in ps],
              "model": llm.name}
    if as_of is not None:
        result["as_of"] = as_of
    if not ps:
        return result
    texts = [f"{p['title']}\n{p['text']}" for p in ps]
    reply = llm.answer(question, texts, history=[(t["question"], t["answer"]) for t in turns])
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
