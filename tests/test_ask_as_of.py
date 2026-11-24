"""`ask` as of a time: answers read items as they were then, and cite those old versions."""

from fastapi.testclient import TestClient

from db4ai import erasure, harness, store
from db4ai.api import create_app
from db4ai.ask import ask, log
from db4ai.db import Provenance, transaction
from db4ai.embeddings import HashEmbedder
from db4ai.llm import Answer, Citation, HeuristicLLM


def save(w, who, title, body, key, team=None):
    prov = Provenance(who.id, "test", "import")
    with transaction(w.conn, prov):
        return store.save_item(w.conn, who, prov, team_id=team or w.eng, type="memory", title=title, body=body,
                               external_key=key)


def now(w):
    return w.conn.execute("select clock_timestamp() as t").fetchone()["t"]


class Scripted:
    name = "scripted"

    def __init__(self, reply):
        self.reply, self.seen = reply, None

    def answer(self, question, passages, history=None):
        self.seen = passages
        return self.reply


def test_ask_as_of_answers_from_and_cites_the_old_version(world):
    w = world
    item = save(w, w.alice, "Atlas launch", "The Atlas launch is on March 14.", "atlas")["item_id"]
    t1 = now(w)
    save(w, w.alice, "Atlas launch", "The Atlas launch slipped to April 30.", "atlas")

    live = ask(w.conn, w.bob, "When is the Atlas launch?", llm=HeuristicLLM(), embedder=HashEmbedder())
    assert "April" in live["answer"] and "as_of" not in live
    old = ask(w.conn, w.bob, "When is the Atlas launch?", llm=HeuristicLLM(), embedder=HashEmbedder(), as_of=t1)
    assert old["answered"] and "March 14" in old["answer"] and "April" not in old["answer"]
    assert old["as_of"] == t1
    assert {(c["item_id"], c["version"]) for c in old["citations"]} == {(item, 1)}
    assert [(s["item_id"], s["version"]) for s in old["sources"]] == [(item, 1)]
    # a quote from the current text isn't in the old passage, so it doesn't ground the answer
    llm = Scripted(Answer(answered=True, answer="April 30.",
                          citations=[Citation(passage=1, quote="slipped to April 30")]))
    res = ask(w.conn, w.bob, "When is the Atlas launch?", llm=llm, embedder=HashEmbedder(), as_of=t1)
    assert not res["answered"] and all("April" not in p for p in llm.seen)


def test_ask_as_of_status_access_and_items_not_yet_created(world):
    w = world
    before = now(w)
    item = save(w, w.alice, "Deploy freeze", "Deploys freeze on Fridays after 2pm.", "freeze")["item_id"]
    t1 = now(w)
    prov = Provenance(w.alice.id, "test", "done")
    with transaction(w.conn, prov):
        store.new_version(w.conn, prov, item, status="archived")
    q = "When do deploys freeze?"
    # archived now: live ask finds nothing, but at t1 it was active
    assert not ask(w.conn, w.bob, q, llm=HeuristicLLM(), embedder=HashEmbedder())["answered"]
    assert ask(w.conn, w.bob, q, llm=HeuristicLLM(), embedder=HashEmbedder(), as_of=t1)["answered"]
    # didn't exist yet; other teams still can't read it
    assert ask(w.conn, w.bob, q, llm=HeuristicLLM(), embedder=HashEmbedder(), as_of=before)["sources"] == []
    assert ask(w.conn, w.carol, q, llm=HeuristicLLM(), embedder=HashEmbedder(), as_of=t1)["sources"] == []


def test_ask_as_of_follow_up_carries_old_versions_and_skips_erased(world):
    w = world
    item = save(w, w.alice, "Atlas launch plan",
                "The Atlas launch moves to March 14. The rollout needs two extra engineers.", "atlas")["item_id"]
    t1 = now(w)
    save(w, w.alice, "Atlas launch plan", "The Atlas launch moves to May 2. The rollout needs five engineers.",
         "atlas")
    turn = {"question": "When is the Atlas launch?", "answer": "March 14.", "citations": [{"item_id": str(item)}]}
    llm = Scripted(Answer(answered=True, answer="Two engineers.",
                          citations=[Citation(passage=1, quote="The rollout needs two extra engineers.")]))
    res = ask(w.conn, w.bob, "And the cost?", llm=llm, embedder=HashEmbedder(), history=[turn], as_of=t1)
    assert res["answered"] and (res["citations"][0]["item_id"], res["citations"][0]["version"]) == (item, 1)
    # carried items respect access as of a time too
    assert ask(w.conn, w.carol, "And the cost?", llm=llm, embedder=HashEmbedder(), history=[turn],
               as_of=t1)["sources"] == []
    # erased since: never comes back, neither by search nor as a carried item
    with w.conn.transaction():
        r = erasure.request(w.conn, w.bob, item, reason="GDPR request")
    harness.decide(w.conn, w.root, r["decision_id"], True)
    res = ask(w.conn, w.bob, "And the cost?", llm=llm, embedder=HashEmbedder(), history=[turn], as_of=t1)
    assert res["sources"] == [] and not res["answered"]
    assert ask(w.conn, w.bob, "When is the Atlas launch?", llm=HeuristicLLM(), embedder=HashEmbedder(),
               as_of=t1)["sources"] == []


def test_ask_as_of_is_logged_and_served_over_http_and_mcp(world, db_url, monkeypatch):
    w = world
    monkeypatch.setenv("DB4AI_LLM", "heuristic")
    monkeypatch.setenv("DB4AI_ASK_LOG", "ids")
    item = save(w, w.alice, "Atlas launch", "The Atlas launch is on March 14.", "atlas")["item_id"]
    t1 = now(w)
    save(w, w.alice, "Atlas launch", "The Atlas launch slipped to April 30.", "atlas")
    with TestClient(create_app(db_url)) as client:
        h = {"Authorization": f"Bearer {w.tokens['bob']}"}
        res = client.post("/ask", headers=h, json={"question": "When is the Atlas launch?",
                                                   "as_of": t1.isoformat()}).json()
        assert "March 14" in res["answer"] and res["citations"][0]["version"] == 1
        assert client.post("/ask", headers=h, json={"question": "x", "as_of": "soon"}).status_code == 400
        client.post("/ask", headers=h, json={"question": "When is the Atlas launch?"})
    rows = log(w.conn, w.root)
    assert sorted((x["as_of"] is not None) for x in rows) == [False, True]
    assert next(x for x in rows if x["as_of"])["as_of"] == t1
    monkeypatch.setenv("DB4AI_TOKEN", w.tokens["bob"])
    from db4ai import mcp_server
    mcp_server._conn = None
    res = mcp_server.ask("When is the Atlas launch?", as_of=t1.isoformat())
    assert res["citations"][0]["version"] == 1 and str(res["citations"][0]["item_id"]) == str(item)
    mcp_server._conn.close()
