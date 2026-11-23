"""'As of' reads: an item, its links and search results as they were at a given time."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from db4ai import erasure, harness, service, store
from db4ai.api import create_app
from db4ai.auth import AccessDenied
from db4ai.db import Provenance, log_change, transaction


def save(w, who, title, body, key, team=None, type="memory"):
    prov = Provenance(who.id, "test", "import")
    with transaction(w.conn, prov):
        return store.save_item(w.conn, who, prov, team_id=team or w.eng, type=type, title=title, body=body,
                               external_key=key)


def now(w):
    return w.conn.execute("select clock_timestamp() as t").fetchone()["t"]


def archive(w, item_id):
    prov = Provenance(w.alice.id, "test", "done")
    with transaction(w.conn, prov):
        store.new_version(w.conn, prov, item_id, status="archived")


def test_get_item_as_of_returns_the_version_then(world):
    w = world
    before = now(w)
    item = save(w, w.alice, "Launch", "Launch is Mar 1", "launch")["item_id"]
    t1 = now(w)
    save(w, w.bob, "Launch", "Launch is Apr 15", "launch")
    t2 = now(w)
    archive(w, item)

    old = store.get_item_as_of(w.conn, w.alice, item, t1)
    assert (old["version"], old["body"], old["status"]) == (1, "Launch is Mar 1", "active")
    assert (old["current_version"], old["current_status"], old["as_of"]) == (3, "archived", t1)
    mid = store.get_item_as_of(w.conn, w.alice, item, t2)
    assert (mid["version"], mid["body"]) == (2, "Launch is Apr 15")
    future = store.get_item_as_of(w.conn, w.alice, item, t2 + timedelta(days=365))
    assert (future["version"], future["status"]) == (3, "archived")

    with pytest.raises(KeyError):                    # did not exist yet
        store.get_item_as_of(w.conn, w.alice, item, before)
    with pytest.raises(AccessDenied):                # team scope applies to the past too
        store.get_item_as_of(w.conn, w.carol, item, t1)


def test_links_as_of_include_deleted_and_exclude_later_ones(world):
    w = world
    a = save(w, w.alice, "Roadmap", "Q3 roadmap", "a")["item_id"]
    b = save(w, w.alice, "Pricing", "New pricing", "b")["item_id"]
    c = save(w, w.alice, "Hiring", "Hiring plan", "c")["item_id"]
    prov = Provenance(w.alice.id, "test", "link")
    with transaction(w.conn, prov):
        store.add_link(w.conn, prov, a, b, "related", 0.9)
    t_ab = now(w)
    with transaction(w.conn, prov):              # remove the a-b link, the way an undone decision does
        link_id = w.conn.execute("select id from links where kind = 'related'").fetchone()["id"]
        w.conn.execute("delete from links where id = %s", (link_id,))
        log_change(w.conn, prov, "link", str(link_id), "delete")
        store.add_link(w.conn, prov, a, c, "mentions", 0.5)
    save(w, w.alice, "Pricing v2", "Newer pricing", "b")
    t_ac = now(w)

    then = store.get_item_as_of(w.conn, w.alice, a, t_ab)["links"]
    assert [(l["other"], l["kind"], l["other_title"]) for l in then] == [(b, "related", "Pricing")]
    later = store.get_item_as_of(w.conn, w.alice, a, t_ac)["links"]
    assert [(l["other"], l["kind"]) for l in later] == [(c, "mentions")]
    assert store.get_item_as_of(w.conn, w.alice, b, t_ac)["links"] == []


def test_search_as_of_matches_old_content_only(world):
    w = world
    item = save(w, w.alice, "Vendor", "We buy from Acme", "vendor")["item_id"]
    t1 = now(w)
    save(w, w.alice, "Vendor", "We switched to Globex", "vendor")
    other = save(w, w.carol, "Acme notes", "Acme sales call", "acme", team=w.sales)["item_id"]
    later = save(w, w.alice, "Acme renewal", "Acme renewal is due", "renew")["item_id"]
    t2 = now(w)
    archive(w, later)

    hits = store.search_as_of(w.conn, w.alice, "acme", t1)
    assert [(h["id"], h["version"], h["chunk"]) for h in hits] == [(item, 1, None)]
    assert store.search_as_of(w.conn, w.alice, "globex", t1) == []
    assert {h["id"] for h in store.search_as_of(w.conn, w.alice, "acme", t2)} == {later}   # vendor moved on
    assert {h["id"] for h in store.search_as_of(w.conn, w.root, "acme", t2)} == {later, other}
    assert store.search_as_of(w.conn, w.alice, "acme renewal", now(w)) == []               # archived now
    assert store.search_as_of(w.conn, w.alice, "acme renewal", now(w), include_inactive=True)[0]["id"] == later
    assert store.search_as_of(w.conn, w.alice, "acme", t2, types=["document"]) == []


def test_erased_items_never_come_back_through_as_of(world):
    w = world
    item = save(w, w.alice, "Zanzibar Quill", "Zanzibar Quill lives at 12 Elm St.", "zq")["item_id"]
    t1 = now(w)
    with w.conn.transaction():
        r = erasure.request(w.conn, w.bob, item, reason="GDPR request")
    harness.decide(w.conn, w.root, r["decision_id"], True)

    assert store.search_as_of(w.conn, w.alice, "zanzibar", t1) == []
    old = store.get_item_as_of(w.conn, w.alice, item, t1)
    assert (old["title"], old["current_status"]) == ("[erased]", "erased")
    assert "Zanzibar" not in str(old)


def test_as_of_over_service_and_http(world, db_url):
    w = world
    item = save(w, w.alice, "Launch", "Launch is Mar 1", "launch")["item_id"]
    t1 = now(w)
    save(w, w.alice, "Launch", "Launch is Apr 15", "launch")

    naive = t1.astimezone(timezone.utc).replace(tzinfo=None).isoformat()   # no offset = UTC
    assert service.get(w.conn, w.alice, str(item), as_of=naive)["version"] == 1
    zulu = t1.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    assert service.search(w.conn, w.alice, "mar", as_of=zulu)[0]["version"] == 1
    with pytest.raises(ValueError):
        service.parse_as_of("last tuesday")
    assert service.parse_as_of(None) is None and service.parse_as_of(datetime(2026, 1, 1)).tzinfo is timezone.utc

    with TestClient(create_app(db_url)) as client:
        h = {"Authorization": f"Bearer {w.tokens['alice']}"}
        r = client.get(f"/items/{item}", params={"as_of": t1.isoformat()}, headers=h).json()
        assert (r["version"], r["body"], r["current_version"]) == (1, "Launch is Mar 1", 2)
        assert client.get(f"/items/{item}", headers=h).json()["version"] == 2
        hits = client.get("/search", params={"q": "mar", "as_of": t1.isoformat()}, headers=h).json()
        assert [x["id"] for x in hits] == [str(item)]
        assert client.get(f"/items/{item}", params={"as_of": "soon"}, headers=h).status_code == 400
        early = (t1 - timedelta(days=1)).isoformat()
        assert client.get(f"/items/{item}", params={"as_of": early}, headers=h).status_code == 404
