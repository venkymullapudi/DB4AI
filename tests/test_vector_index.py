"""Fixed dims per embedding model, per-model HNSW indexes (pgvector) and candidate-pool search."""

import pytest

from db4ai import store
from db4ai.db import Provenance, migrate, transaction, vector_enabled
from db4ai.embeddings import HashEmbedder
from db4ai.worker import run_until_idle


def save(w, actor, team, title, body):
    prov = Provenance(actor.id, "test")
    with transaction(w.conn, prov):
        return store.save_item(w.conn, actor, prov, team_id=team, type="note", title=title, body=body)["item_id"]


def test_model_dims_are_fixed_on_first_use(world):
    w = world
    i = save(w, w.alice, w.eng, "Note", "hello")
    with transaction(w.conn):
        store.put_embedding(w.conn, i, "m1", 1, [1.0, 0.0, 0.0])
    assert store.model_dims(w.conn, "m1") == 3
    with pytest.raises(ValueError, match="3 dimensions, got 2"):
        with transaction(w.conn):
            store.put_chunks(w.conn, i, "m1", 1, "h", [(0, 5)], [[1.0, 0.0]])
    with pytest.raises(ValueError, match="query has 2"):
        store.similar(w.conn, [1.0, 0.0], "m1", team_ids=None)
    assert store.similar(w.conn, [1.0, 0.0], "unknown-model", team_ids=None) == []


def test_enrichment_registers_model_and_builds_indexes(world):
    w = world
    save(w, w.alice, w.eng, "Roadmap", "We plan the Frankfurt migration.")
    run_until_idle(w.ctx)
    emb = HashEmbedder()
    assert store.model_dims(w.conn, emb.name) == emb.dims
    idx = {r["indexname"] for r in w.conn.execute(
        "select indexname from pg_indexes where indexname like 'item\\_%%\\_hnsw\\_%%'")}
    assert len(idx) == (2 if vector_enabled(w.conn) else 0)


def test_hnsw_index_is_used_for_candidates(world):
    w = world
    if not vector_enabled(w.conn):
        pytest.skip("pgvector not installed")
    for n in range(5):
        save(w, w.alice, w.eng, f"Note {n}", f"topic {n} about databases")
    run_until_idle(w.ctx)
    emb = HashEmbedder()
    lit = "[" + ",".join(f"{x:.6f}" for x in emb.embed(["databases"])[0]) + "]"
    with w.conn.transaction():
        w.conn.execute("set local enable_seqscan = off")
        plan = "\n".join(r["QUERY PLAN"] for r in w.conn.execute(
            f"explain select item_id from item_chunks where model = '{emb.name}'"
            f" order by embedding::vector({emb.dims}) <=> '{lit}'::vector({emb.dims}) limit 10"))
    assert "item_chunks_hnsw_" in plan


def test_small_candidate_pool_falls_back_to_exact_results(world, monkeypatch):
    """Other teams' closer chunks fill the pool; the filtered search must still find our team's items."""
    w = world
    for n in range(12):
        save(w, w.carol, w.sales, f"Sales {n}", "Quarterly pipeline review for the Frankfurt datacenter deal.")
    bodies = ["Frankfurt datacenter cutover plan for the Postgres replicas.",
              "Budget review: quarterly hardware spend, racks and cooling.",
              "Pipeline of hiring interviews for the platform team."]
    ours = [save(w, w.alice, w.eng, f"Eng {n}", body) for n, body in enumerate(bodies)]
    run_until_idle(w.ctx)
    emb = HashEmbedder()
    q = "Quarterly pipeline review Frankfurt datacenter"
    monkeypatch.setenv("DB4AI_VECTOR_CANDIDATES", "1000")
    exact = store.search(w.conn, w.alice, q, embedder=emb, limit=3)
    monkeypatch.setenv("DB4AI_VECTOR_CANDIDATES", "1")
    small = store.search(w.conn, w.alice, q, embedder=emb, limit=3)
    assert {h["id"] for h in small} == {h["id"] for h in exact} == set(ours)
    assert [h["score"] for h in small] == [h["score"] for h in exact]
    vec = emb.embed([q])[0]
    assert [r["id"] for r in store.similar(w.conn, vec, emb.name, team_ids=[w.eng], limit=3)] == \
        [h["id"] for h in exact]


def test_migration_backfills_registry_and_drops_off_size_rows(world):
    w = world
    a = save(w, w.alice, w.eng, "A", "alpha")
    b = save(w, w.alice, w.eng, "B", "beta")
    c = w.conn
    c.execute("drop table embedding_models")
    c.execute("drop function db4ai_ensure_vector_index(text, int)")
    c.execute("drop function db4ai_embedding_is_vector()")
    c.execute("delete from schema_migrations where name = '004_vector_index.sql'")
    vec = vector_enabled(c)

    def put(item, model, v, chunk=None):
        val = ("[" + ",".join(map(str, v)) + "]") if vec else v
        cast = "vector" if vec else "float8[]"
        if chunk is None:
            c.execute(f"insert into item_embeddings (item_id, model, version, embedding) values (%s,%s,1,%s::{cast})",
                      (item, model, val))
        else:
            c.execute("insert into item_chunks (item_id, model, chunk_no, version, content_hash, start_char, end_char,"
                      f" embedding) values (%s,%s,%s,1,'h',0,0,%s::{cast})", (item, model, chunk, val))

    put(a, "m", [1.0, 0.0, 0.0])
    put(a, "m", [1.0, 0.0, 0.0], chunk=0)
    put(b, "m", [1.0, 0.0], chunk=0)        # off-size: dropped
    put(b, "m2", [0.0, 1.0])
    assert migrate(c) == ["004_vector_index.sql"]
    assert store.model_dims(c, "m") == 3 and store.model_dims(c, "m2") == 2
    assert c.execute("select count(*) n from item_chunks where model='m'").fetchone()["n"] == 1
    assert c.execute("select count(*) n from item_embeddings").fetchone()["n"] == 2
    idx = c.execute("select count(*) n from pg_indexes where indexname like 'item\\_%%\\_hnsw\\_%%'").fetchone()["n"]
    assert idx == (4 if vec else 0)
