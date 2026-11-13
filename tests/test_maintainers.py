from db4ai import store
from db4ai.db import Provenance, transaction
from db4ai.worker import run_until_idle, schedule_periodic


def save(w, title, body, type="memory", meta=None, team=None, who=None):
    who = who or w.alice
    prov = Provenance(who.id, "test")
    with transaction(w.conn, prov):
        return store.save_item(w.conn, who, prov, team_id=team or w.eng, type=type, title=title, body=body,
                               metadata=meta)["item_id"]


def decisions(w, action=None):
    q = "select action_type, status, tier, payload from harness_decisions"
    return [d for d in w.conn.execute(q).fetchall() if action is None or d["action_type"] == action]


def test_enrich_tags_summarises_and_embeds(world):
    w = world
    i = save(w, "Q3 pricing review", "We reviewed enterprise pricing with Priya Shah. Enterprise pricing rises 10% in Q3.")
    run_until_idle(w.ctx)
    cur = store.get_item(w.conn, w.alice, i)
    ai = cur["metadata"]["ai"]
    assert "pricing" in ai["tags"] and ai["summary"].startswith("We reviewed")
    assert "Priya Shah" in ai["entities"]
    assert w.conn.execute("select count(*) n from item_embeddings where item_id=%s", (i,)).fetchone()["n"] == 1
    # enrichment is a new version by the agent; content (and hash) unchanged
    hist = store.history(w.conn, w.alice, i)
    assert hist[-1]["actor_kind"] == "agent" and len(hist) >= 2


def test_related_items_get_linked(world):
    w = world
    a = save(w, "Kubernetes migration plan", "Plan to migrate billing services to Kubernetes clusters in eu-west.")
    b = save(w, "Kubernetes migration risks", "Risks of migrating billing services to Kubernetes clusters: eu-west capacity.")
    save(w, "Team offsite lunch", "Lunch menu ideas for the offsite: tacos, salad.")
    run_until_idle(w.ctx)
    links = w.conn.execute("select src_item, dst_item from links where kind='related'").fetchall()
    assert {frozenset((l["src_item"], l["dst_item"])) for l in links} == {frozenset((a, b))}


def test_near_duplicates_are_merged_with_notice(world):
    w = world
    body = "Acme Corp is our largest customer in the logistics vertical; renewal owner is Dana."
    a = save(w, "Acme Corp", body)
    b = save(w, "Acme Corp.", body + " ")
    run_until_idle(w.ctx)
    merges = decisions(w, "merge_items")
    assert len(merges) == 1 and merges[0]["status"] == "applied_auto" and merges[0]["tier"] == "auto_notice"
    statuses = {store.get_item(w.conn, w.alice, x)["status"] for x in (a, b)}
    assert statuses == {"active", "merged"}


def test_newer_item_marks_contradicted_memory_stale(world):
    w = world
    old = save(w, "Launch date", "The product launch date is 2026-03-01 for the Atlas release.")
    w.conn.execute("select pg_sleep(0.01)")
    save(w, "Atlas launch moved", "Atlas release product launch date moved to 2026-04-15 after review.", type="meeting")
    run_until_idle(w.ctx)
    stale = decisions(w, "mark_stale")
    assert stale and stale[0]["payload"]["item_id"] == str(old)
    assert store.get_item(w.conn, w.alice, old)["status"] == "stale"


def test_schema_scan_proposes_promotion_once(world):
    w = world
    for n in range(5):
        save(w, f"Sales call {n}", "call notes", type="meeting", meta={"customer": f"C{n}", "deal_size": 1000 * n})
    schedule_periodic(w.conn)
    run_until_idle(w.ctx)
    props = decisions(w, "promote_schema")
    assert len(props) == 1 and props[0]["status"] == "pending"
    assert {f["name"] for f in props[0]["payload"]["fields"]} == {"customer", "deal_size"}
    schedule_periodic(w.conn)
    run_until_idle(w.ctx)
    assert len(decisions(w, "promote_schema")) == 1   # pending proposal is not duplicated


def test_failed_job_retries_then_fails(world, monkeypatch):
    w = world
    from db4ai import maintainers

    def boom(ctx, payload):
        raise RuntimeError("model unavailable")

    monkeypatch.setitem(maintainers.HANDLERS, "enrich", boom)
    save(w, "x", "y")
    run_until_idle(w.ctx)
    job = w.conn.execute("select status, attempts, last_error from jobs where kind='enrich'").fetchone()
    assert job["status"] == "queued" and job["attempts"] == 1 and "model unavailable" in job["last_error"]
    w.conn.execute("update jobs set run_after=now(), attempts=max_attempts-1 where kind='enrich'")
    run_until_idle(w.ctx)
    assert w.conn.execute("select status from jobs where kind='enrich'").fetchone()["status"] == "failed"


def test_hygiene_removes_orphan_blobs_and_expires_decisions(world):
    w = world
    w.blobs.put("deadbeef" * 8, b"orphan from a rolled-back transaction")
    w.conn.execute("insert into harness_decisions (action_type, payload, tier, fingerprint, status, proposed_by, expires_at)"
                   " select 'archive_item', '{}', 'ask_first', 'fp', 'pending', id, now() - interval '1 day' from actors limit 1")
    from db4ai import maintainers
    from db4ai.blobs import gc_orphans

    assert gc_orphans(w.conn, w.blobs) == []   # within the grace period: kept
    commit = maintainers.hygiene(w.ctx, {})
    out = commit(w.conn)
    assert out["expired"] == 1
    assert gc_orphans(w.conn, w.blobs, grace_seconds=0) == ["deadbeef" * 8]
