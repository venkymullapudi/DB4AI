import psycopg
import pytest

from db4ai import store
from db4ai.auth import AccessDenied
from db4ai.db import Provenance, transaction


def save(w, actor, **kw):
    prov = Provenance(actor.id, "test", kw.pop("reason", None))
    with transaction(w.conn, prov):
        return store.save_item(w.conn, actor, prov, **kw)


def test_versions_are_append_only_with_provenance(world):
    w = world
    r1 = save(w, w.alice, team_id=w.eng, type="memory", title="Launch", body="Launch is Mar 1", external_key="launch")
    r2 = save(w, w.bob, team_id=w.eng, type="memory", title="Launch", body="Launch is Apr 15", external_key="launch",
              reason="moved after review")
    assert r1["item_id"] == r2["item_id"] and (r1["version"], r2["version"]) == (1, 2)
    hist = store.history(w.conn, w.alice, r1["item_id"])
    assert [h["actor"] for h in hist] == ["alice", "bob"]
    assert hist[1]["reason"] == "moved after review"

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        w.conn.execute("update item_versions set body='tampered' where item_id=%s", (r1["item_id"],))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        w.conn.execute("delete from change_log")


def test_resave_identical_content_is_idempotent(world):
    w = world
    a = save(w, w.alice, team_id=w.eng, type="document", title="Spec", body="x", external_key="spec.md")
    b = save(w, w.alice, team_id=w.eng, type="document", title="Spec", body="x", external_key="spec.md")
    assert b == {"item_id": a["item_id"], "version": 1, "changed": False}


def test_revert_creates_new_version(world):
    w = world
    r = save(w, w.alice, team_id=w.eng, type="memory", title="t", body="v1", external_key="k")
    save(w, w.alice, team_id=w.eng, type="memory", title="t", body="v2 wrong", external_key="k")
    prov = Provenance(w.alice.id, "test", "undo")
    with transaction(w.conn, prov):
        out = store.revert(w.conn, w.alice, prov, r["item_id"], 1)
    assert out["version"] == 3
    assert store.get_item(w.conn, w.alice, r["item_id"])["body"] == "v1"


def test_transaction_rollback_leaves_no_trace(world):
    """ACID: a failed transaction writes neither the item, its version, its change_log nor its job."""
    w = world
    before = {t: w.conn.execute(f"select count(*) n from {t}").fetchone()["n"]
              for t in ("items", "item_versions", "change_log", "jobs")}
    prov = Provenance(w.alice.id, "test")
    with pytest.raises(RuntimeError):
        with transaction(w.conn, prov):
            store.save_item(w.conn, w.alice, prov, team_id=w.eng, type="memory", title="t", body="b")
            raise RuntimeError("crash mid-write")
    after = {t: w.conn.execute(f"select count(*) n from {t}").fetchone()["n"] for t in before}
    assert after == before


def test_team_isolation(world):
    w = world
    r = save(w, w.alice, team_id=w.eng, type="memory", title="Secret roadmap", body="roadmap details")
    with pytest.raises(AccessDenied):
        store.get_item(w.conn, w.carol, r["item_id"])
    with pytest.raises(AccessDenied):
        save(w, w.carol, team_id=w.eng, type="memory", title="x", body="y")
    assert store.search(w.conn, w.carol, "roadmap") == []
    assert store.search(w.conn, w.bob, "roadmap")[0]["id"] == r["item_id"]
    assert store.get_item(w.conn, w.root, r["item_id"])["title"] == "Secret roadmap"


def test_write_enqueues_enrich_job_in_same_transaction(world):
    w = world
    r = save(w, w.alice, team_id=w.eng, type="memory", title="t", body="b")
    job = w.conn.execute("select * from jobs where kind='enrich'").fetchone()
    assert job["payload"] == {"item_id": str(r["item_id"])}
