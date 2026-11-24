"""Per-item ACLs: an item restricted to named readers within its team, enforced on every read path."""

import pytest
from fastapi.testclient import TestClient

from db4ai import auth, harness, service, store
from db4ai.api import create_app
from db4ai.auth import AccessDenied
from db4ai.db import Provenance, transaction
from db4ai.llm import HeuristicLLM
from db4ai.worker import run_until_idle


def save(w, who, title, body, key=None, readers=None, type="memory", meta=None):
    prov = Provenance(who.id, "test", "import")
    with transaction(w.conn, prov):
        return store.save_item(w.conn, who, prov, team_id=w.eng, type=type, title=title, body=body,
                               external_key=key, readers=readers, metadata=meta)["item_id"]


def add_dave(w):
    """A second eng member (dave), alongside bob."""
    with w.conn.transaction():
        dave = auth.create_actor(w.conn, "dave")
        auth.add_member(w.conn, w.eng, dave, "member")
    return auth.load_actor(w.conn, dave)


def restrict(w, item, readers, who=None, approve_as=None):
    d = service.propose_access(w.conn, who or w.alice, str(item), readers, reason="sensitive")
    assert d["status"] == "pending" and d["tier"] == "ask_first"
    return harness.decide(w.conn, approve_as or w.alice, d["decision_id"], approve=True)


def test_restricted_item_is_hidden_from_other_team_members(world):
    w = world
    dave = add_dave(w)
    item = save(w, w.alice, "Reorg plan", "Confidential reorg of the platform group", key="reorg")
    other = save(w, w.alice, "Platform roadmap", "Platform group roadmap")
    with transaction(w.conn, Provenance(w.alice.id, "test")):
        store.add_link(w.conn, Provenance(w.alice.id, "test"), other, item)
    assert store.get_item(w.conn, w.bob, other)["links"]                 # visible before the restriction

    restrict(w, item, ["dave"])

    # Dave (a reader), Alice (team owner) and root (admin) read it; Bob (member) gets "not found".
    for who in (dave, w.alice, w.root):
        assert store.get_item(w.conn, who, item)["readers"] == ["dave"]
        assert [h["id"] for h in store.search(w.conn, who, "reorg")] == [item]
    with pytest.raises(KeyError):
        store.get_item(w.conn, w.bob, item)
    with pytest.raises(KeyError):
        store.history(w.conn, w.bob, item)
    with pytest.raises(KeyError):
        store.access(w.conn, w.bob, item)
    assert store.search(w.conn, w.bob, "reorg") == []
    assert store.get_item(w.conn, w.bob, other)["links"] == []           # no title leak through links
    with pytest.raises(AccessDenied):                                    # outsiders still get 403
        store.get_item(w.conn, w.carol, item)

    # As-of reads use today's ACL.
    t = w.conn.execute("select clock_timestamp() as t").fetchone()["t"]
    assert store.search_as_of(w.conn, w.bob, "reorg", t) == []
    with pytest.raises(KeyError):
        store.get_item_as_of(w.conn, w.bob, item, t)
    assert [h["id"] for h in store.search_as_of(w.conn, dave, "reorg", t)] == [item]


def test_writes_and_requests_need_read_access(world):
    w = world
    item = save(w, w.alice, "Salary bands", "Bands for 2027", key="bands", readers=[])
    assert store.item_readers(w.conn, item) == [w.alice.id]             # the creator is always a reader
    with pytest.raises(AccessDenied):                                    # upsert by key can't overwrite it
        save(w, w.bob, "Salary bands", "Bob's edit", key="bands")
    with pytest.raises(KeyError):
        service.revert(w.conn, w.bob, str(item), 1)
    with pytest.raises(KeyError):
        service.request_erasure(w.conn, w.bob, str(item), "gdpr")
    with pytest.raises(KeyError):
        service.propose_access(w.conn, w.bob, str(item), None, reason="let me in")
    with pytest.raises(ValueError):                                      # readers only apply at creation
        save(w, w.alice, "Salary bands", "Bands for 2027 v2", key="bands", readers=[w.bob.id])
    with pytest.raises(ValueError):                                      # readers must be team members
        save(w, w.alice, "Carol only", "x", readers=[w.carol.id])


def test_access_changes_are_ask_first_revertible_and_repeatable(world):
    w = world
    with w.conn.transaction():                                           # even an "auto" policy can't skip review
        harness.set_policy(w.conn, w.root, "set_item_access", "auto")
    item = save(w, w.alice, "Board deck", "Q4 board deck")
    with pytest.raises(AccessDenied):                                    # members can't approve
        restrict(w, item, ["bob"], who=w.bob, approve_as=w.bob)
    pending = harness.list_decisions(w.conn, w.alice)
    assert pending[0]["diff"]["readers"] == [None, ["bob"]]
    harness.decide(w.conn, w.alice, pending[0]["id"], approve=True)
    assert store.access(w.conn, w.bob, item)["readers"] == ["bob"]

    lift = restrict(w, item, None)
    assert store.item_readers(w.conn, item) is None
    harness.revert(w.conn, w.root, lift["decision_id"])                  # revert restores the previous ACL
    assert store.item_readers(w.conn, item) == [w.bob.id]
    restrict(w, item, None)                                              # same request again is a new decision
    restrict(w, item, ["bob"])
    assert store.item_readers(w.conn, item) == [w.bob.id]
    log = w.conn.execute("select op from change_log where entity = 'item_acl' order by id").fetchall()
    assert [r["op"] for r in log] == ["set", "lift", "set", "lift", "set"]
    with pytest.raises(ValueError):
        service.propose_access(w.conn, w.alice, str(item), ["nobody"], reason="typo")
    with pytest.raises(ValueError):
        service.propose_access(w.conn, w.alice, str(item), ["bob"], reason=" ")


def test_decisions_about_hidden_items_are_hidden(world):
    w = world
    dave = add_dave(w)
    item = save(w, w.alice, "Layoffs", "Layoff list draft", readers=[dave.id])
    run_until_idle(w.ctx)                                                # enrich proposes about the item
    w.conn.execute("update policies set tier = 'ask_first' where action_type = 'archive_item'")
    with transaction(w.conn):
        harness.propose(w.conn, w.alice, "archive_item", {"item_id": str(item)}, reason="done")
    mine = {d["action_type"] for d in harness.list_decisions(w.conn, dave)}
    assert "archive_item" in mine
    assert harness.list_decisions(w.conn, w.bob) == []
    assert harness.list_decisions(w.conn, w.bob, status="applied_auto") == []
    assert harness.list_decisions(w.conn, dave, status="applied_auto")


def test_maintainers_never_pair_restricted_items(world):
    w = world
    body = "Acme Corp is our largest customer in the logistics vertical; renewal owner is Dana."
    secret = save(w, w.alice, "Acme Corp", body, readers=[])
    save(w, w.alice, "Acme Corp.", body + " ")
    run_until_idle(w.ctx)
    acts = {d["action_type"] for d in w.conn.execute("select action_type from harness_decisions").fetchall()}
    assert "merge_items" not in acts and "add_link" not in acts
    assert store.get_item(w.conn, w.alice, secret)["metadata"]["ai"]["tags"]   # still enriched for its readers
    other = save(w, w.alice, "Beta Corp", "Beta")
    with transaction(w.conn), pytest.raises(ValueError):                 # a direct merge across ACLs is refused
        harness.propose(w.conn, w.root, "merge_items", {"keep": str(other), "drop": str(secret)},
                        reason="x", confidence=1.0)


def test_schema_promotion_skips_restricted_items(world):
    w = world
    for i in range(3):
        save(w, w.alice, f"Deal {i}", "deal", type="note", meta={"amount": 5})
    save(w, w.alice, "Deal secret", "deal", type="note", readers=[], meta={"amount": 999})
    with transaction(w.conn):
        harness.propose(w.conn, w.root, "promote_schema",
                        {"team_id": str(w.eng), "item_type": "note", "dataset_name": "deals",
                         "fields": [{"name": "amount", "type": "bigint"}], "item_count": 3}, reason="x")
    d = harness.list_decisions(w.conn, w.root)[0]
    harness.decide(w.conn, w.root, d["id"], approve=True)
    rows = service.dataset_rows(w.conn, w.bob, "deals", "eng")
    assert sorted(r["amount"] for r in rows) == [5, 5, 5]


def test_ask_ignores_restricted_items_and_carried_ids(world):
    w = world
    secret = save(w, w.alice, "Acquisition target", "We plan to acquire Zephyr Labs in March.", readers=[])
    r = service.ask(w.conn, w.bob, "Who do we plan to acquire?", llm=HeuristicLLM())
    assert r["citations"] == []
    hist = [{"question": "q", "answer": "a", "citations": [{"item_id": str(secret)}]}]
    r = service.ask(w.conn, w.bob, "When do we acquire them?", llm=HeuristicLLM(), history=hist)
    assert all(c["item_id"] != str(secret) for c in r["citations"])
    r = service.ask(w.conn, w.alice, "Who do we plan to acquire?", llm=HeuristicLLM())
    assert [c["item_id"] for c in r["citations"]] == [str(secret)]


def test_http_access_endpoints(world, db_url):
    w = world
    with TestClient(create_app(db_url)) as client:
        ha = {"Authorization": f"Bearer {w.tokens['alice']}"}
        hb = {"Authorization": f"Bearer {w.tokens['bob']}"}
        item = client.post("/items", json={"type": "memory", "title": "Offer", "body": "Offer letter",
                                           "team": "eng", "readers": []}, headers=ha).json()["item_id"]
        assert client.get(f"/items/{item}", headers=hb).status_code == 404
        acc = client.get(f"/items/{item}/access", headers=ha).json()
        assert acc == {"item_id": item, "restricted": True, "readers": ["alice"], "owners": ["alice"]}
        d = client.post(f"/items/{item}/access", json={"readers": ["alice", "bob"], "reason": "share"},
                        headers=ha).json()
        assert d["status"] == "pending"
        client.post(f"/harness/decisions/{d['decision_id']}", json={"approve": True}, headers=ha)
        assert client.get(f"/items/{item}", headers=hb).json()["readers"] == ["alice", "bob"]
        assert client.post(f"/items/{item}/access", json={"readers": ["ghost"], "reason": "x"},
                           headers=ha).status_code == 400
        assert client.post("/items", json={"type": "memory", "title": "X", "team": "eng", "readers": ["carol"]},
                           headers=ha).status_code == 400
