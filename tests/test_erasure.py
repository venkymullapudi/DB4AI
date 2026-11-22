import psycopg
import pytest
from fastapi.testclient import TestClient

from db4ai import erasure, harness, store, worker
from db4ai.api import create_app
from db4ai.auth import AccessDenied
from db4ai.blobs import store_blob
from db4ai.db import Provenance, transaction
from db4ai.harness import HarnessError

LEAK_TABLES = ("item_versions", "items", "change_log", "harness_decisions", "links", "jobs", "erasures")


def leaks(conn, needle: str) -> dict:
    """Tables with any row whose JSON form contains `needle` (case-insensitive)."""
    out = {}
    for t in LEAK_TABLES:
        n = conn.execute(f"select count(*) n from {t} x where to_jsonb(x)::text ilike %s", (f"%{needle}%",)).fetchone()["n"]
        if n:
            out[t] = n
    return out


def save(w, who, title, body, key=None, blob=None, team=None):
    prov = Provenance(who.id, "test", "import")
    with transaction(w.conn, prov):
        return store.save_item(w.conn, who, prov, team_id=team or w.eng, type="memory", title=title, body=body,
                               external_key=key, blob_sha256=blob)


def request(w, who, item_id, reason="GDPR request #42", terms=None):
    with w.conn.transaction():
        return erasure.request(w.conn, who, item_id, reason=reason, terms=terms)


def test_erase_scrubs_every_version_and_keeps_a_tombstone(world):
    w = world
    with w.conn.transaction():
        sha = store_blob(w.conn, w.blobs, b"Zanzibar passport scan", "application/pdf")
    first = save(w, w.alice, "Zanzibar Quill profile", "Zanzibar Quill lives at 12 Elm St.", key="people/zq.md", blob=sha)
    item = first["item_id"]
    save(w, w.bob, "Zanzibar Quill profile", "Zanzibar Quill moved to 9 Oak Ave.", key="people/zq.md", blob=sha)
    other = save(w, w.alice, "Quill offsite", "Offsite in May at Elm park.")["item_id"]
    worker.run_until_idle(w.ctx)  # enrichment versions, embeddings, chunks, links, decisions
    assert leaks(w.conn, "zanzibar")

    r = request(w, w.bob, item)                      # any reader may ask
    assert (r["tier"], r["status"]) == ("ask_first", "pending")
    assert store.get_item(w.conn, w.alice, item)["title"] == "Zanzibar Quill profile"  # nothing yet
    with pytest.raises(AccessDenied):                # team owners can't approve erasure: admins only
        harness.decide(w.conn, w.alice, r["decision_id"], True)

    out = harness.decide(w.conn, w.root, r["decision_id"], True, note="verified identity")
    assert out["status"] == "approved" and out["result"]["blobs_deleted"] == 1

    cur = store.get_item(w.conn, w.alice, item)
    assert (cur["status"], cur["title"], cur["body"], cur["blob_sha256"]) == ("erased", "[erased]", "", None)
    hist = store.history(w.conn, w.alice, item)
    assert [h["actor"] for h in hist[:2]] == ["alice", "bob"]       # who did what survives
    assert all(h["title"] == "[erased]" for h in hist)
    assert hist[-1]["harness_decision_id"] == r["decision_id"]

    # The item's content is gone from every table; the other item is untouched.
    assert leaks(w.conn, "zanzibar") == {} and leaks(w.conn, "elm st") == {} and leaks(w.conn, "oak ave") == {}
    assert store.get_item(w.conn, w.alice, other)["body"] == "Offsite in May at Elm park."
    for t in ("item_embeddings", "item_chunks"):
        assert w.conn.execute(f"select count(*) n from {t} where item_id = %s", (item,)).fetchone()["n"] == 0
    assert [h["id"] for h in store.search(w.conn, w.alice, "Elm")] == [other]

    tomb = erasure.erasures(w.conn, w.alice, item)
    assert len(tomb) == 1 and tomb[0]["kind"] == "erase" and tomb[0]["reason"] == "GDPR request #42"
    assert (tomb[0]["requested_by"], tomb[0]["approved_by"], tomb[0]["versions"]) == ("bob", "root", len(hist) - 1)

    # Blob row is gone in the same transaction; the file goes via the outbox job.
    assert w.conn.execute("select 1 from blobs where sha256 = %s", (sha,)).fetchone() is None
    assert w.blobs.exists(sha)
    worker.run_until_idle(w.ctx)
    assert not w.blobs.exists(sha)

    # The erased item accepts no writes, and re-importing its key doesn't resurrect it.
    again = save(w, w.alice, "Zanzibar Quill profile", "Zanzibar Quill lives at 12 Elm St.", key="people/zq.md")
    assert again["erased"] and not again["changed"] and again["item_id"] == item
    with pytest.raises(ValueError):
        with transaction(w.conn):
            store.revert(w.conn, w.alice, Provenance(w.alice.id, "test"), item, 1)
    with pytest.raises(HarnessError):
        harness.revert(w.conn, w.root, r["decision_id"])


def test_shared_blob_is_kept(world):
    w = world
    with w.conn.transaction():
        sha = store_blob(w.conn, w.blobs, b"shared bytes")
    a = save(w, w.alice, "A", "a", blob=sha)["item_id"]
    save(w, w.alice, "B", "b", blob=sha)
    r = request(w, w.alice, a)
    out = harness.decide(w.conn, w.root, r["decision_id"], True)
    assert (out["result"]["blobs_deleted"], out["result"]["blobs_kept"]) == (0, 1)
    assert w.conn.execute("select 1 from blobs where sha256 = %s", (sha,)).fetchone() is not None


def test_redact_replaces_terms_everywhere_and_re_enriches(world):
    w = world
    item = save(w, w.alice, "Call with Jane Doe", "Jane Doe's number is 555-0199. Renewal due in March.",
                key="calls/jd.md")["item_id"]
    save(w, w.alice, "Call with Jane Doe", "JANE DOE's number is 555-0199. Renewal due in March; she agreed.",
         key="calls/jd.md")
    worker.run_until_idle(w.ctx)

    r = request(w, w.bob, item, reason="remove Jane Doe's phone", terms=["Jane Doe", "555-0199", " "])
    pending = harness.list_decisions(w.conn, w.root)[0]
    assert pending["diff"]["terms"]["Jane Doe"] >= 4 and pending["diff"]["terms"]["555-0199"] >= 2
    out = harness.decide(w.conn, w.root, r["decision_id"], True)
    assert out["result"]["matches"] >= 6

    for needle in ("jane doe", "555-0199"):
        assert leaks(w.conn, needle) == {}, needle
    cur = store.get_item(w.conn, w.alice, item)
    assert cur["status"] == "active" and cur["title"] == "Call with [redacted]"
    assert cur["body"].startswith("[redacted]'s number is [redacted]. Renewal due in March")
    assert cur["metadata"]["redactions"][0]["terms"] == 2
    assert erasure.erasures(w.conn, w.alice, item)[0]["reason"] == "remove [redacted]'s phone"

    # Derived data was dropped and is rebuilt from the redacted content.
    assert w.conn.execute("select count(*) n from item_chunks where item_id=%s", (item,)).fetchone()["n"] == 0
    worker.run_until_idle(w.ctx)
    assert w.conn.execute("select count(*) n from item_chunks where item_id=%s", (item,)).fetchone()["n"] > 0
    assert leaks(w.conn, "jane doe") == {}
    assert store.search(w.conn, w.alice, "renewal")[0]["id"] == item


def test_policy_cannot_loosen_erasure(world):
    w = world
    harness.set_policy(w.conn, w.root, "erase_item", "auto")
    item = save(w, w.alice, "x", "y")["item_id"]
    r = request(w, w.alice, item)
    assert (r["tier"], r["status"]) == ("ask_first", "pending")


def test_rejected_request_changes_nothing(world):
    w = world
    item = save(w, w.alice, "Keep me", "Important.")["item_id"]
    r = request(w, w.alice, item, reason="mistake")
    assert harness.decide(w.conn, w.root, r["decision_id"], False)["status"] == "rejected"
    assert store.get_item(w.conn, w.alice, item)["body"] == "Important."
    assert erasure.erasures(w.conn, w.alice, item) == []
    with pytest.raises(ValueError):
        request(w, w.alice, item, reason="  ")
    with pytest.raises(AccessDenied):
        request(w, w.carol, item)  # other team can't even ask


def test_append_only_bypass_is_narrow(world):
    w = world
    a = save(w, w.alice, "A", "secret a")["item_id"]
    b = save(w, w.alice, "B", "secret b")["item_id"]
    r = request(w, w.alice, a)
    with w.conn.transaction():
        archive = harness.propose(w.conn, w.alice, "archive_item", {"item_id": str(b)}, reason="x")

    def attempt(setting, sql, params):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with w.conn.transaction():
                w.conn.execute("select set_config('db4ai.erasure', %s, true)", (setting,))
                w.conn.execute(sql, params)

    attempt("", "update item_versions set body = '' where item_id = %s", (a,))                       # no erasure
    attempt(str(archive["decision_id"]), "update item_versions set body = '' where item_id = %s", (b,))  # wrong action
    attempt(str(r["decision_id"]), "update item_versions set body = '' where item_id = %s", (b,))      # other item
    attempt(str(r["decision_id"]), "update item_versions set actor_id = %s where item_id = %s", (w.bob.id, a))  # provenance
    attempt(str(r["decision_id"]), "delete from item_versions where item_id = %s", (a,))               # never delete
    with w.conn.transaction():  # the one allowed shape: content of the target item, during its erasure
        w.conn.execute("select set_config('db4ai.erasure', %s, true)", (str(r["decision_id"]),))
        w.conn.execute("update item_versions set body = 'x' where item_id = %s", (a,))
    harness.decide(w.conn, w.root, r["decision_id"], False)
    attempt(str(r["decision_id"]), "update item_versions set body = '' where item_id = %s", (a,))      # decided now


def test_http_erasure_flow(world, db_url):
    w = world
    item = save(w, w.alice, "Alpha", "alpha body")["item_id"]
    with TestClient(create_app(db_url)) as client:
        alice = {"Authorization": f"Bearer {w.tokens['alice']}"}
        root = {"Authorization": f"Bearer {w.tokens['root']}"}
        assert client.post(f"/items/{item}/redact", headers=alice, json={"reason": "x", "terms": []}).status_code == 400
        d = client.post(f"/items/{item}/erase", headers=alice, json={"reason": "legal hold ended"}).json()
        assert d["status"] == "pending"
        assert client.post(f"/harness/decisions/{d['decision_id']}", headers=alice,
                           json={"approve": True}).status_code == 403
        assert client.post(f"/harness/decisions/{d['decision_id']}", headers=root,
                           json={"approve": True}).json()["status"] == "approved"
        tomb = client.get(f"/items/{item}/erasures", headers=alice).json()
        assert tomb[0]["kind"] == "erase" and tomb[0]["approved_by"] == "root"
        assert client.get(f"/items/{item}", headers=alice).json()["status"] == "erased"


def test_edited_redaction_terms_are_scrubbed_too(world):
    w = world
    item = save(w, w.alice, "Notes", "Codename Bluefin ships in Q3.")["item_id"]
    r = request(w, w.alice, item, reason="codename leak", terms=["Q3"])
    edited = {"item_id": str(item), "reason": "codename leak", "terms": ["Bluefin"]}
    harness.decide(w.conn, w.root, r["decision_id"], True, edited_payload=edited)
    assert leaks(w.conn, "bluefin") == {}
    assert store.get_item(w.conn, w.alice, item)["body"] == "Codename [redacted] ships in Q3."
