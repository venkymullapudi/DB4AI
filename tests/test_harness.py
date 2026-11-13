import pytest

from db4ai import harness, store
from db4ai.auth import AccessDenied, maintainer_agent
from db4ai.db import Provenance, transaction
from db4ai.harness import HarnessError


def item(w, title="Acme renewal", body="Acme renewal is due 2026-03-01.", type="memory", team=None):
    who = w.carol if team == w.sales else w.alice
    prov = Provenance(who.id, "test")
    with transaction(w.conn, prov):
        return store.save_item(w.conn, who, prov, team_id=team or w.eng, type=type, title=title, body=body)["item_id"]


def agent(w):
    with w.conn.transaction():
        return maintainer_agent(w.conn)


def propose(w, action, payload, confidence=None):
    with w.conn.transaction():
        return harness.propose(w.conn, agent(w), action, payload, reason="test", confidence=confidence)


def test_auto_tier_applies_immediately(world):
    w = world
    i = item(w)
    r = propose(w, "add_tags", {"item_id": str(i), "value": ["acme", "renewal"]}, confidence=1.0)
    assert (r["tier"], r["status"]) == ("auto", "applied_auto")
    cur = store.get_item(w.conn, w.alice, i)
    assert cur["metadata"]["ai"]["tags"] == ["acme", "renewal"]
    v = store.history(w.conn, w.alice, i)[-1]
    assert v["actor_kind"] == "agent" and v["harness_decision_id"] == r["decision_id"]


def test_ask_first_waits_for_owner_and_applies_atomically(world):
    w = world
    i = item(w)
    r = propose(w, "archive_item", {"item_id": str(i)})
    assert (r["tier"], r["status"]) == ("ask_first", "pending")
    assert store.get_item(w.conn, w.alice, i)["status"] == "active"   # nothing happened yet

    with pytest.raises(AccessDenied):            # members cannot decide; only owners/admins
        harness.decide(w.conn, w.bob, r["decision_id"], True)
    with pytest.raises(AccessDenied):            # other team's people cannot either
        harness.decide(w.conn, w.carol, r["decision_id"], True)

    out = harness.decide(w.conn, w.alice, r["decision_id"], True, note="obsolete")
    assert out["status"] == "approved"
    assert store.get_item(w.conn, w.alice, i)["status"] == "archived"
    v = store.history(w.conn, w.alice, i)[-1]
    assert (v["actor"], v["harness_decision_id"]) == ("alice", r["decision_id"])
    with pytest.raises(HarnessError):
        harness.decide(w.conn, w.alice, r["decision_id"], True)  # already decided


def test_failed_apply_rolls_back_the_decision(world, monkeypatch):
    """Approval and the change commit together: if applying fails, the decision stays pending."""
    w = world
    i = item(w)
    r = propose(w, "archive_item", {"item_id": str(i)})
    from db4ai import actions

    def boom(conn, prov, p):
        store.new_version(conn, prov, p["item_id"], status="archived")
        raise RuntimeError("apply failed halfway")

    orig = actions.REGISTRY["archive_item"]
    monkeypatch.setitem(actions.REGISTRY, "archive_item", actions.Action(orig.team_of, orig.diff, boom, orig.revert))
    with pytest.raises(RuntimeError):
        harness.decide(w.conn, w.alice, r["decision_id"], True)
    d = w.conn.execute("select status from harness_decisions where id=%s", (r["decision_id"],)).fetchone()
    assert d["status"] == "pending"
    assert store.get_item(w.conn, w.alice, i)["status"] == "active"


def test_rejection_is_remembered(world):
    w = world
    i = item(w)
    r = propose(w, "archive_item", {"item_id": str(i)})
    harness.decide(w.conn, w.alice, r["decision_id"], False, note="still needed")
    again = propose(w, "archive_item", {"item_id": str(i)})
    assert again["skipped"] and again["status"] == "rejected"


def test_expired_decision_is_not_applied(world):
    w = world
    i = item(w)
    r = propose(w, "archive_item", {"item_id": str(i)})
    w.conn.execute("update harness_decisions set expires_at = now() - interval '1 minute' where id=%s", (r["decision_id"],))
    out = harness.decide(w.conn, w.alice, r["decision_id"], True)
    assert out["status"] == "expired"
    assert store.get_item(w.conn, w.alice, i)["status"] == "active"


def test_unknown_action_fails_closed(world):
    w = world
    r = propose(w, "drop_table", {"table": "data.everything"})
    assert (r["tier"], r["status"]) == ("ask_first", "pending")
    out = harness.decide(w.conn, w.root, r["decision_id"], True)
    assert out["status"] == "rejected"    # cannot be applied even if someone clicks approve


def test_low_confidence_escalates_to_ask_first(world):
    w = world
    a, b = item(w, "Acme Inc", "Acme Inc customer"), item(w, "ACME", "ACME customer")
    r = propose(w, "merge_items", {"keep": str(a), "drop": str(b)}, confidence=0.6)
    assert r["tier"] == "ask_first"
    hi = propose(w, "merge_items", {"keep": str(a), "drop": str(b), "x": 1}, confidence=0.99)
    assert hi["tier"] == "auto_notice" and hi["status"] == "applied_auto"


def test_auto_notice_can_be_reverted_within_window(world):
    w = world
    a, b = item(w, "Acme Inc", "Acme Inc customer"), item(w, "ACME", "ACME customer")
    r = propose(w, "merge_items", {"keep": str(a), "drop": str(b)}, confidence=0.99)
    assert store.get_item(w.conn, w.alice, b)["status"] == "merged"
    harness.revert(w.conn, w.bob, r["decision_id"], note="different companies")
    assert store.get_item(w.conn, w.alice, b)["status"] == "active"
    assert "aliases" not in store.get_item(w.conn, w.alice, a)["metadata"]
    assert w.conn.execute("select count(*) n from links where kind='duplicate_of'").fetchone()["n"] == 0
    # reverted proposals are never re-proposed
    assert propose(w, "merge_items", {"keep": str(a), "drop": str(b)}, confidence=0.99)["skipped"]


def test_revert_refuses_when_item_changed_since(world):
    w = world
    i = item(w)
    r = propose(w, "mark_stale", {"item_id": str(i), "explanation": "x"}, confidence=0.9)
    prov = Provenance(w.alice.id, "test")
    with transaction(w.conn, prov):
        store.new_version(w.conn, prov, i, body="human edited after")
    with pytest.raises(HarnessError, match="changed after"):
        harness.revert(w.conn, w.alice, r["decision_id"])


def test_team_policy_overrides_global(world):
    w = world
    harness.set_policy(w.conn, w.alice, "add_tags", "ask_first", team_id=w.eng)
    i = item(w)
    assert propose(w, "add_tags", {"item_id": str(i), "value": ["x"]}, 1.0)["tier"] == "ask_first"
    j = item(w, team=w.sales)
    assert propose(w, "add_tags", {"item_id": str(j), "value": ["x"]}, 1.0)["tier"] == "auto"
    with pytest.raises(AccessDenied):
        harness.set_policy(w.conn, w.bob, "add_tags", "auto", team_id=w.eng)


def test_promote_schema_creates_bi_table_with_change_capture(world):
    w = world
    for n in range(3):
        prov = Provenance(w.alice.id, "test")
        with transaction(w.conn, prov):
            store.save_item(w.conn, w.alice, prov, team_id=w.eng, type="meeting", title=f"Standup {n}",
                            body="notes", metadata={"attendees": ["a", "b"], "duration_min": 15 + n})
    payload = {"team_id": str(w.eng), "item_type": "meeting", "dataset_name": "meeting_fields",
               "fields": [{"name": "attendees", "type": "jsonb"}, {"name": "duration_min", "type": "bigint"}]}
    r = propose(w, "promote_schema", payload)
    assert r["status"] == "pending"
    out = harness.decide(w.conn, w.root, r["decision_id"], True)
    assert out["result"]["rows"] == 3
    rows = w.conn.execute("select duration_min from data.eng_meeting_fields order by duration_min").fetchall()
    assert [x["duration_min"] for x in rows] == [15, 16, 17]
    log = w.conn.execute("select actor_id, harness_decision_id from change_log where entity='data.eng_meeting_fields'").fetchall()
    assert len(log) == 3 and all(l["harness_decision_id"] == r["decision_id"] for l in log)
