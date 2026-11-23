import asyncio

from fastapi.testclient import TestClient

from db4ai import store
from db4ai.api import create_app
from db4ai.ask import NOT_FOUND, ask
from db4ai.db import Provenance, transaction
from db4ai.embeddings import HashEmbedder
from db4ai.llm import Answer, Citation, HeuristicLLM
from db4ai.worker import run_until_idle


def save(w, actor, team, title, body, key=None, type="memory"):
    prov = Provenance(actor.id, "test")
    with transaction(w.conn, prov):
        return store.save_item(w.conn, actor, prov, team_id=team, type=type, title=title, body=body,
                               external_key=key)


def _transcript():
    filler = [f"Paragraph {i}: we discussed the weekly status of hiring, budgets and office logistics." for i in range(60)]
    filler.insert(37, "Priya proposed moving the Postgres replicas to the Frankfurt datacenter before the audit.")
    return "\n\n".join(filler)


class Scripted:
    """An LLM that returns a fixed reply, to test how `ask` validates what the model says."""
    name = "scripted"

    def __init__(self, reply):
        self.reply, self.seen = reply, None

    def answer(self, question, passages):
        self.seen = passages
        return self.reply


def test_ask_answers_with_item_and_version_citation(world):
    w = world
    r = save(w, w.alice, w.eng, "Weekly all-hands", _transcript(), type="meeting")
    run_until_idle(w.ctx)  # chunks + enrichment (adds a metadata-only version)
    version = store.get_item(w.conn, w.alice, r["item_id"])["version"]
    res = ask(w.conn, w.bob, "Where are the Postgres replicas moving?", llm=HeuristicLLM(), embedder=HashEmbedder())
    assert res["answered"] and "Frankfurt" in res["answer"]
    cite = res["citations"][0]
    assert cite["item_id"] == r["item_id"] and cite["version"] == version
    assert "Frankfurt datacenter" in cite["quote"]
    assert res["sources"][0]["item_id"] == r["item_id"]


def test_ask_respects_team_access_and_fails_closed(world):
    w = world
    save(w, w.alice, w.eng, "Deploy freeze", "Deploys freeze on Fridays after 2pm.")
    run_until_idle(w.ctx)
    # carol (sales) can't read eng items: nothing to ground on
    res = ask(w.conn, w.carol, "When do deploys freeze?", llm=HeuristicLLM(), embedder=HashEmbedder())
    assert not res["answered"] and res["answer"] == NOT_FOUND and res["citations"] == [] and res["sources"] == []
    # unrelated question: sources may be found but nothing answers it
    res = ask(w.conn, w.bob, "Who owns the marketing budget?", llm=HeuristicLLM(), embedder=HashEmbedder())
    assert not res["answered"] and res["citations"] == []


def test_ask_drops_invented_citations(world):
    w = world
    r = save(w, w.alice, w.eng, "Deploy freeze", "Deploys freeze on Fridays after 2pm.")
    run_until_idle(w.ctx)
    q = "When do deploys freeze?"
    # passage 7 doesn't exist and the quote isn't in passage 1: not grounded
    llm = Scripted(Answer(answered=True, answer="On Mondays.", citations=[
        Citation(passage=7, quote="Deploys freeze on Fridays"), Citation(passage=1, quote="freeze on Mondays")]))
    res = ask(w.conn, w.bob, q, llm=llm, embedder=HashEmbedder())
    assert not res["answered"] and res["answer"] == NOT_FOUND and res["citations"] == []
    assert "Deploys freeze on Fridays" in llm.seen[0]
    # a verbatim quote (case and whitespace aside) grounds it; the bad one is kept without its quote
    llm = Scripted(Answer(answered=True, answer="Fridays after 2pm.", citations=[
        Citation(passage=1, quote="deploys  freeze on fridays"), Citation(passage=1, quote="made up")]))
    res = ask(w.conn, w.bob, q, llm=llm, embedder=HashEmbedder())
    assert res["answered"] and res["answer"] == "Fridays after 2pm."
    assert [c["quote"] for c in res["citations"]] == ["deploys  freeze on fridays", None]
    assert res["citations"][0]["item_id"] == r["item_id"] and res["citations"][0]["version"] >= 1
    # the model saying it didn't answer wins, whatever it cites
    llm = Scripted(Answer(answered=False, answer="Maybe Fridays.", citations=[
        Citation(passage=1, quote="Deploys freeze on Fridays")]))
    assert not ask(w.conn, w.bob, q, llm=llm, embedder=HashEmbedder())["answered"]


def test_ask_uses_best_window_for_fulltext_only_hits_and_writes_nothing(world):
    w = world
    r = save(w, w.alice, w.eng, "Runbook", _transcript())  # no worker run: no chunks, found by full-text only
    before = w.conn.execute("select (select count(*) from item_versions) v, (select count(*) from harness_decisions) d").fetchone()
    res = ask(w.conn, w.bob, "Postgres replicas Frankfurt", llm=HeuristicLLM(), embedder=HashEmbedder())
    assert res["answered"] and res["citations"][0]["item_id"] == r["item_id"] and res["citations"][0]["version"] == 1
    after = w.conn.execute("select (select count(*) from item_versions) v, (select count(*) from harness_decisions) d").fetchone()
    assert before == after


def test_ask_rejects_empty_question(world):
    import pytest
    with pytest.raises(ValueError):
        ask(world.conn, world.bob, "   ", llm=HeuristicLLM(), embedder=HashEmbedder())


def test_ask_over_http_and_mcp(world, db_url, monkeypatch):
    w = world
    monkeypatch.setenv("DB4AI_LLM", "heuristic")
    r = save(w, w.alice, w.eng, "Deploy freeze", "Deploys freeze on Fridays after 2pm.")
    with TestClient(create_app(db_url)) as client:
        assert client.post("/ask", json={"question": "x"}).status_code == 401
        h = {"Authorization": f"Bearer {w.tokens['bob']}"}
        res = client.post("/ask", headers=h, json={"question": "When do deploys freeze?"}).json()
        assert res["answered"] and res["citations"][0]["item_id"] == str(r["item_id"])
        assert client.post("/ask", headers=h, json={"question": ""}).status_code == 400
    monkeypatch.setenv("DB4AI_TOKEN", w.tokens["bob"])
    from db4ai import mcp_server
    mcp_server._conn = None
    assert "ask" in {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    res = mcp_server.ask("When do deploys freeze?")
    assert res["answered"] and res["citations"][0]["version"] == 1
    mcp_server._conn.close()


def _log_rows(w):
    return w.conn.execute("select * from ask_log order by asked_at, id").fetchall()


def test_ask_log_is_off_by_default_and_rejects_unknown_modes(world, monkeypatch):
    import pytest
    w = world
    save(w, w.alice, w.eng, "Deploy freeze", "Deploys freeze on Fridays after 2pm.")
    monkeypatch.delenv("DB4AI_ASK_LOG", raising=False)
    assert ask(w.conn, w.bob, "When do deploys freeze?", llm=HeuristicLLM(), embedder=HashEmbedder())["answered"]
    assert _log_rows(w) == []
    monkeypatch.setenv("DB4AI_ASK_LOG", "everything")
    with pytest.raises(ValueError):  # misconfigured: no answer rather than an unlogged (or over-logged) one
        ask(w.conn, w.bob, "When do deploys freeze?", llm=HeuristicLLM(), embedder=HashEmbedder())
    assert _log_rows(w) == []


def test_ask_log_ids_mode_records_who_read_which_versions(world, monkeypatch):
    from db4ai.ask import log, question_hash
    w = world
    monkeypatch.setenv("DB4AI_ASK_LOG", "ids")
    r = save(w, w.alice, w.eng, "Deploy freeze", "Deploys freeze on Fridays after 2pm.")
    res = ask(w.conn, w.bob, "When do deploys freeze?", llm=HeuristicLLM(), embedder=HashEmbedder())
    ask(w.conn, w.carol, "When do deploys freeze?", llm=HeuristicLLM(), embedder=HashEmbedder())  # sees nothing
    rows = _log_rows(w)
    assert len(rows) == 2
    bob = rows[0]
    assert bob["actor_id"] == w.bob.id and bob["answered"] and bob["question"] is None
    assert bob["question_sha256"] == question_hash("  when do DEPLOYS freeze? ")
    assert bob["citations"] == [{"item_id": str(r["item_id"]), "version": res["citations"][0]["version"]}]
    assert r["item_id"] in bob["source_items"] and bob["cited_items"] == [r["item_id"]]
    assert "Fridays" not in str(bob)  # no answer, quote or title stored
    assert not rows[1]["answered"] and rows[1]["sources"] == []
    # admins see everything; others only their own rows; filters by actor and item
    assert len(log(w.conn, w.root)) == 2
    assert [x["actor"] for x in log(w.conn, w.bob)] == ["bob"]
    assert [x["actor"] for x in log(w.conn, w.root, actor_name="carol")] == ["carol"]
    assert [x["actor"] for x in log(w.conn, w.root, item_id=r["item_id"])] == ["bob"]
    # append-only
    import psycopg
    import pytest
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        w.conn.execute("update ask_log set answered = false")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        w.conn.execute("delete from ask_log")


def test_ask_log_write_failure_returns_no_answer(world, monkeypatch):
    import pytest
    from db4ai import ask as ask_mod
    w = world
    monkeypatch.setenv("DB4AI_ASK_LOG", "text")
    save(w, w.alice, w.eng, "Deploy freeze", "Deploys freeze on Fridays after 2pm.")

    def boom(*a, **k):
        raise RuntimeError("log down")
    monkeypatch.setattr(ask_mod, "_log", boom)
    with pytest.raises(RuntimeError):
        ask(w.conn, w.bob, "When do deploys freeze?", llm=HeuristicLLM(), embedder=HashEmbedder())


def test_ask_log_text_is_scrubbed_by_erasure_and_redaction(world, monkeypatch):
    from db4ai import erasure, harness
    w = world
    monkeypatch.setenv("DB4AI_ASK_LOG", "text")
    a = save(w, w.alice, w.eng, "Zanzibar Quill profile", "Zanzibar Quill lives at 12 Elm St.")["item_id"]
    b = save(w, w.alice, w.eng, "Deploy freeze", "Deploys freeze on Fridays after 2pm. Ask Yolanda Brisk.")["item_id"]
    ask(w.conn, w.bob, "Where does Zanzibar Quill live?", llm=HeuristicLLM(), embedder=HashEmbedder())
    ask(w.conn, w.bob, "Yolanda Brisk deploy freeze?", llm=HeuristicLLM(), embedder=HashEmbedder())
    rows = _log_rows(w)
    assert a in rows[0]["source_items"] and b in rows[1]["source_items"]
    assert rows[0]["question"] == "Where does Zanzibar Quill live?"

    with w.conn.transaction():
        d = erasure.request(w.conn, w.bob, b, reason="GDPR", terms=["Yolanda Brisk"])
    harness.decide(w.conn, w.root, d["decision_id"], True)
    with w.conn.transaction():
        d = erasure.request(w.conn, w.bob, a, reason="GDPR")
    harness.decide(w.conn, w.root, d["decision_id"], True)

    after = {r["id"]: r for r in _log_rows(w)}
    for r in rows:
        x = after[r["id"]]
        # ids, versions and the hash survive; only the question text changes
        assert (x["question_sha256"], x["sources"], x["citations"]) == (r["question_sha256"], r["sources"], r["citations"])
    assert a not in rows[1]["source_items"]
    assert after[rows[1]["id"]]["question"] == "[redacted] deploy freeze?"
    assert after[rows[0]["id"]]["question"] is None


def test_ask_log_over_http(world, db_url, monkeypatch):
    w = world
    monkeypatch.setenv("DB4AI_LLM", "heuristic")
    monkeypatch.setenv("DB4AI_ASK_LOG", "ids")
    r = save(w, w.alice, w.eng, "Deploy freeze", "Deploys freeze on Fridays after 2pm.")
    with TestClient(create_app(db_url)) as client:
        hb, hr = ({"Authorization": f"Bearer {w.tokens[n]}"} for n in ("bob", "root"))
        client.post("/ask", headers=hb, json={"question": "When do deploys freeze?"})
        client.post("/ask", headers=hr, json={"question": "When do deploys freeze?"})
        assert [x["actor"] for x in client.get("/ask/log", headers=hb).json()] == ["bob"]
        assert len(client.get("/ask/log", headers=hr).json()) == 2
        got = client.get("/ask/log", headers=hr, params={"actor": "bob", "item": str(r["item_id"])}).json()
        assert [x["actor"] for x in got] == ["bob"] and got[0]["citations"][0]["item_id"] == str(r["item_id"])
        assert client.get("/ask/log").status_code == 401
