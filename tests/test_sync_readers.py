"""Readers on synced records: a connector batch can create items restricted to the source's readers."""

import pytest
from fastapi.testclient import TestClient

from db4ai import auth, connectors, harness, store
from db4ai.api import create_app
from db4ai.connectors import Record


def add_dave(w):
    with w.conn.transaction():
        dave = auth.create_actor(w.conn, "dave")
        auth.add_member(w.conn, w.eng, dave, "member")
    return auth.load_actor(w.conn, dave)


def sync(w, records, cursor, expected, who=None):
    return connectors.apply_batch(w.conn, who or w.alice, "drive", w.eng, records, cursor, expected)


def rec(readers, body="Confidential reorg"):
    return Record(key="f1", type="document", title="Reorg plan", body=body, readers=readers)


def test_new_synced_item_is_restricted_and_unknown_readers_are_dropped(world):
    w = world
    dave = add_dave(w)
    out = sync(w, [rec(["dave", "carol", "nobody"]),
                   Record(key="f2", type="document", title="Roadmap", body="Public roadmap")], "c1", None)
    item = out["items"][0]["item_id"]
    assert out["items"][0]["readers_dropped"] == ["carol", "nobody"]      # carol is in sales: access only narrows
    assert out["access_proposed"] == 0
    assert sorted(store.get_item(w.conn, dave, item)["readers"]) == ["alice", "dave"]   # the syncer stays a reader
    with pytest.raises(KeyError):
        store.get_item(w.conn, w.bob, item)
    assert store.item_readers(w.conn, out["items"][1]["item_id"]) is None              # readers=None: team-wide

    # Re-sending the same readers changes nothing and proposes nothing.
    again = sync(w, [rec(["dave", "carol"])], "c2", "c1")
    assert again["changed"] == 0 and again["access_proposed"] == 0


def test_changed_readers_propose_set_item_access(world):
    w = world
    add_dave(w)
    item = sync(w, [rec(["dave"])], "c1", None)["items"][0]["item_id"]

    out = sync(w, [rec(["dave", "bob"])], "c2", "c1")
    d = out["items"][0]["access_decision"]
    assert d["status"] == "pending" and out["access_proposed"] == 1
    with pytest.raises(KeyError):                                         # not applied until approved
        store.get_item(w.conn, w.bob, item)
    # The same batch again is the same decision; a newer change supersedes it.
    assert sync(w, [rec(["dave", "bob"])], "c3", "c2")["items"][0]["access_decision"]["decision_id"] == d["decision_id"]
    newer = sync(w, [rec(["bob"])], "c4", "c3")["items"][0]["access_decision"]
    assert newer["decision_id"] != d["decision_id"]
    old = w.conn.execute("select status from harness_decisions where id=%s", (d["decision_id"],)).fetchone()
    assert old["status"] == "expired"

    harness.decide(w.conn, w.alice, newer["decision_id"], approve=True)
    assert sorted(store.get_item(w.conn, w.bob, item)["readers"]) == ["alice", "bob"]
    assert sync(w, [rec(["bob"])], "c5", "c4")["access_proposed"] == 0     # in line with the source now

    # Moving back to the item's ACL withdraws a pending change.
    p = sync(w, [rec(["dave"])], "c6", "c5")["items"][0]["access_decision"]
    assert sync(w, [rec(["bob"])], "c7", "c6")["access_proposed"] == 0
    assert w.conn.execute("select status from harness_decisions where id=%s",
                          (p["decision_id"],)).fetchone()["status"] == "expired"


def test_rejected_reader_change_is_not_reproposed(world):
    w = world
    add_dave(w)
    sync(w, [rec(["dave"])], "c1", None)
    d = sync(w, [rec(["dave", "bob"])], "c2", "c1")["items"][0]["access_decision"]
    harness.decide(w.conn, w.alice, d["decision_id"], approve=False)
    again = sync(w, [rec(["dave", "bob"])], "c3", "c2")["items"][0]["access_decision"]
    assert again["decision_id"] == d["decision_id"] and again["status"] == "rejected"


def test_readers_on_existing_team_wide_item_propose_a_restriction(world):
    w = world
    item = sync(w, [rec(None)], "c1", None)["items"][0]["item_id"]
    d = sync(w, [rec(["bob"])], "c2", "c1")["items"][0]["access_decision"]
    assert d["status"] == "pending"
    assert store.get_item(w.conn, w.bob, item)["title"] == "Reorg plan"   # still team-wide until approved
    # readers=None never changes access.
    assert sync(w, [rec(None, body="v2")], "c3", "c2")["access_proposed"] == 0


def test_http_sync_with_readers(world, db_url):
    w = world
    add_dave(w)
    h = {"Authorization": f"Bearer {w.tokens['alice']}"}
    body = {"items": [{"key": "f1", "type": "document", "title": "Reorg", "body": "secret", "readers": ["dave"]}],
            "cursor": "t1", "expected_cursor": None}
    with TestClient(create_app(db_url)) as client:
        r = client.post("/sync/drive", json=body, headers=h)
        assert r.status_code == 200
        item = r.json()["items"][0]["item_id"]
        bob = {"Authorization": f"Bearer {w.tokens['bob']}"}
        assert client.get(f"/items/{item}", headers=bob).status_code == 404


def test_public_record_proposes_lifting_a_restriction(world):
    w = world
    add_dave(w)
    item = sync(w, [rec(["dave"])], "c1", None)["items"][0]["item_id"]

    out = sync(w, [Record(key="f1", type="document", title="Reorg plan", body="Confidential reorg", public=True)],
               "c2", "c1")
    d = out["items"][0]["access_decision"]
    assert d["status"] == "pending" and out["access_proposed"] == 1
    row = w.conn.execute("select payload, reason from harness_decisions where id=%s", (d["decision_id"],)).fetchone()
    assert row["payload"]["readers"] is None and "public" in row["reason"]
    with pytest.raises(KeyError):                                         # still restricted until approved
        store.get_item(w.conn, w.bob, item)
    # Re-sent: the same decision. Restricted again in the source before approval: the lift is withdrawn.
    pub = Record(key="f1", type="document", title="Reorg plan", body="Confidential reorg", public=True)
    assert sync(w, [pub], "c3", "c2")["items"][0]["access_decision"]["decision_id"] == d["decision_id"]
    assert "access_decision" not in sync(w, [rec(["dave"])], "c4", "c3")["items"][0]
    assert w.conn.execute("select status from harness_decisions where id=%s",
                          (d["decision_id"],)).fetchone()["status"] == "expired"

    # Public again: a new proposal; approving it lifts the ACL, and later public syncs propose nothing.
    again = sync(w, [pub], "c5", "c4")["items"][0]["access_decision"]
    harness.decide(w.conn, w.alice, again["decision_id"], approve=True)
    assert store.item_readers(w.conn, item) is None
    assert store.get_item(w.conn, w.bob, item)["id"] == item
    assert sync(w, [pub], "c6", "c5")["access_proposed"] == 0


def test_public_new_item_is_team_wide_and_public_with_readers_is_rejected(world):
    w = world
    out = sync(w, [Record(key="f9", type="document", title="Handbook", body="Company handbook", public=True)],
               "c1", None)
    assert store.item_readers(w.conn, out["items"][0]["item_id"]) is None and out["access_proposed"] == 0
    with pytest.raises(ValueError, match="public"):
        sync(w, [Record(key="f8", type="document", title="X", body="x", public=True, readers=["bob"])], "c2", "c1")
    assert connectors.get_cursor(w.conn, w.alice, "drive", w.eng)["cursor"] == "c1"   # nothing written
