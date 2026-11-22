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
