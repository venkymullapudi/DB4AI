"""Sharing an item with another team or actor: read-only access outside the item's team, owner-approved."""

import pytest
from fastapi.testclient import TestClient

from db4ai import auth, harness, service, store
from db4ai.api import create_app
from db4ai.auth import AccessDenied
from db4ai.db import Provenance, transaction
from db4ai.llm import HeuristicLLM
from db4ai.worker import run_until_idle


def save(w, who, title, body, key=None, readers=None, type="memory"):
    prov = Provenance(who.id, "test", "import")
    with transaction(w.conn, prov):
        return store.save_item(w.conn, who, prov, team_id=w.eng, type=type, title=title, body=body,
                               external_key=key, readers=readers)["item_id"]


def share(w, item, teams=(), actors=(), who=None, approve_as=None):
    d = service.propose_shares(w.conn, who or w.alice, str(item), list(teams), list(actors), reason="cross-team")
    assert d["status"] == "pending" and d["tier"] == "ask_first"
    return harness.decide(w.conn, approve_as or w.alice, d["decision_id"], approve=True)


def outsider(w, name="erin"):
    """An actor in no team at all."""
    with w.conn.transaction():
        x = auth.create_actor(w.conn, name)
    return auth.load_actor(w.conn, x)


def test_shared_item_is_readable_by_the_other_team(world):
    w = world
    item = save(w, w.alice, "Pricing memo", "Enterprise pricing for the zephyr launch", key="pricing")
    other = save(w, w.alice, "Internal note", "Eng only zephyr note")
    with transaction(w.conn, Provenance(w.alice.id, "test")):
        store.add_link(w.conn, Provenance(w.alice.id, "test"), item, other)
    with pytest.raises(AccessDenied):                                    # not shared yet: outsiders get 403
        store.get_item(w.conn, w.carol, item)
    assert store.search(w.conn, w.carol, "zephyr") == []

    share(w, item, teams=["sales"])

    got = store.get_item(w.conn, w.carol, item)
    assert got["title"] == "Pricing memo"
    assert got["links"] == []                                            # the unshared other end stays hidden
    assert "readers" not in got and "shared_with" not in got             # no names from the other team
    assert store.history(w.conn, w.carol, item)
    assert [h["id"] for h in store.search(w.conn, w.carol, "zephyr", embedder=w.ctx.embedder)] == [item]
    t = w.conn.execute("select clock_timestamp() as t").fetchone()["t"]
    assert store.get_item_as_of(w.conn, w.carol, item, t)["version"] == 1
    assert [h["id"] for h in store.search_as_of(w.conn, w.carol, "zephyr", t)] == [item]
    r = service.ask(w.conn, w.carol, "What is the enterprise pricing for zephyr?", llm=HeuristicLLM())
    assert [c["item_id"] for c in r["citations"]] == [str(item)]
    # The team still sees who it's shared with; the access listing is team-only.
    assert store.get_item(w.conn, w.bob, item)["shared_with"] == {"teams": ["sales"], "actors": []}
    with pytest.raises(AccessDenied):
        store.access(w.conn, w.carol, item)


def test_sharing_is_read_only(world):
    w = world
    item = save(w, w.alice, "Plan", "Q1 plan", key="plan")
    share(w, item, teams=["sales"])
    with pytest.raises(AccessDenied):
        service.revert(w.conn, w.carol, str(item), 1)
    with pytest.raises(AccessDenied):
        service.request_erasure(w.conn, w.carol, str(item), "gdpr")
    with pytest.raises(AccessDenied):
        service.propose_access(w.conn, w.carol, str(item), None, reason="x")
    with pytest.raises(AccessDenied):
        service.propose_shares(w.conn, w.carol, str(item), [], [], reason="unshare it")
    assert harness.list_decisions(w.conn, w.carol, status="applied") == []


def test_actor_shares_and_restricted_items(world):
    w = world
    erin = outsider(w)
    item = save(w, w.alice, "Board deck", "Confidential board deck", readers=[])
    share(w, item, actors=["erin"])
    assert store.get_item(w.conn, erin, item)["title"] == "Board deck"   # an owner-approved grant
    with pytest.raises(KeyError):                                        # the ACL still hides it from bob
        store.get_item(w.conn, w.bob, item)
    with pytest.raises(AccessDenied):                                    # carol (sales) isn't named
        store.get_item(w.conn, w.carol, item)
    for teams, actors in ((["eng"], []), ([], ["bob"]), (["nowhere"], []), ([], ["ghost"])):
        with pytest.raises(ValueError):                                  # own team, members, unknown names
            service.propose_shares(w.conn, w.alice, str(item), teams, actors, reason="x")
    with pytest.raises(ValueError):
        service.propose_shares(w.conn, w.alice, str(item), ["sales"], [], reason=" ")


def test_shares_are_ask_first_revertible_and_logged(world):
    w = world
    with w.conn.transaction():                                           # even an "auto" policy can't skip review
        harness.set_policy(w.conn, w.root, "set_item_shares", "auto")
    item = save(w, w.alice, "Spec", "Shared spec")
    d = service.propose_shares(w.conn, w.bob, str(item), ["sales"], [], reason="sales needs it")
    with pytest.raises(AccessDenied):                                    # members can't approve
        harness.decide(w.conn, w.bob, d["decision_id"], approve=True)
    assert harness.list_decisions(w.conn, w.alice)[0]["diff"]["teams"] == [[], ["sales"]]
    harness.decide(w.conn, w.alice, d["decision_id"], approve=True)
    assert store.get_item(w.conn, w.carol, item)

    unshare = share(w, item)
    assert store.item_shares(w.conn, item) is None
    with pytest.raises(AccessDenied):
        store.get_item(w.conn, w.carol, item)
    harness.revert(w.conn, w.root, unshare["decision_id"])               # revert restores the share
    assert store.get_item(w.conn, w.carol, item)
    share(w, item)                                                       # the same request again is a new decision
    log = w.conn.execute("select op from change_log where entity = 'item_shares' order by id").fetchall()
    assert [r["op"] for r in log] == ["set", "unshare", "set", "unshare"]


def test_maintainers_never_pair_shared_items(world):
    w = world
    body = "Acme Corp is our largest customer in the logistics vertical; renewal owner is Dana."
    shared = save(w, w.alice, "Acme Corp", body)
    share(w, shared, teams=["sales"])
    save(w, w.alice, "Acme Corp.", body + " ")
    run_until_idle(w.ctx)
    acts = {d["action_type"] for d in w.conn.execute("select action_type from harness_decisions").fetchall()}
    assert "merge_items" not in acts and "add_link" not in acts
    other = save(w, w.alice, "Beta Corp", "Beta")
    with transaction(w.conn), pytest.raises(ValueError):                 # a direct merge across shares is refused
        harness.propose(w.conn, w.root, "merge_items", {"keep": str(other), "drop": str(shared)},
                        reason="x", confidence=1.0)


def test_http_shares_endpoint(world, db_url):
    w = world
    with TestClient(create_app(db_url)) as client:
        ha = {"Authorization": f"Bearer {w.tokens['alice']}"}
        hc = {"Authorization": f"Bearer {w.tokens['carol']}"}
        item = client.post("/items", json={"type": "memory", "title": "Launch", "body": "Launch plan",
                                           "team": "eng"}, headers=ha).json()["item_id"]
        assert client.get(f"/items/{item}", headers=hc).status_code == 403
        d = client.post(f"/items/{item}/shares", json={"teams": ["sales"], "reason": "launch"}, headers=ha).json()
        assert d["status"] == "pending"
        client.post(f"/harness/decisions/{d['decision_id']}", json={"approve": True}, headers=ha)
        assert client.get(f"/items/{item}", headers=hc).json()["title"] == "Launch"
        assert client.get(f"/items/{item}/access", headers=ha).json()["shared_with"] == {"teams": ["sales"],
                                                                                         "actors": []}
        assert client.post(f"/items/{item}/shares", json={"teams": ["eng"], "reason": "x"},
                           headers=ha).status_code == 400
        assert client.post(f"/items/{item}/shares", json={"reason": "x"}, headers=hc).status_code == 403
