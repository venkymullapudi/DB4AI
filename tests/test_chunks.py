from db4ai import store
from db4ai.db import Provenance, transaction
from db4ai.embeddings import HashEmbedder, chunk_spans
from db4ai.worker import run_until_idle


def save(w, title, body, key=None):
    prov = Provenance(w.alice.id, "test")
    with transaction(w.conn, prov):
        return store.save_item(w.conn, w.alice, prov, team_id=w.eng, type="meeting", title=title, body=body,
                               external_key=key)["item_id"]


def test_chunk_spans_pack_paragraphs_and_cut_long_ones():
    text = "alpha beta\n\ngamma delta\n\n" + "word " * 100
    spans = chunk_spans(text, 40)
    assert all(b - a <= 40 for a, b in spans)
    assert text[spans[0][0]:spans[0][1]] == "alpha beta\n\ngamma delta"
    # every non-space character is covered by exactly one chunk, in order
    covered = "".join(text[a:b] for a, b in spans).replace(" ", "").replace("\n", "")
    assert covered == text.replace(" ", "").replace("\n", "")
    assert chunk_spans("", 40) == [(0, 0)] and chunk_spans("short", 40) == [(0, 5)]


def _transcript():
    filler = [f"Paragraph {i}: we discussed the weekly status of hiring, budgets and office logistics." for i in range(60)]
    filler.insert(37, "Priya proposed moving the Postgres replicas to the Frankfurt datacenter before the audit.")
    return "\n\n".join(filler)


def test_long_item_is_chunked_and_search_returns_the_matching_chunk(world):
    w = world
    body = _transcript()
    i = save(w, "Weekly all-hands", body)
    run_until_idle(w.ctx)
    n = w.conn.execute("select count(*) n from item_chunks where item_id=%s", (i,)).fetchone()["n"]
    assert n == len(chunk_spans(body, 1500)) and n > 1
    hits = store.search(w.conn, w.alice, "Postgres replicas Frankfurt datacenter", embedder=HashEmbedder())
    assert hits[0]["id"] == i
    chunk = hits[0]["chunk"]
    assert "Frankfurt datacenter" in chunk["text"] and chunk["text"] == body[chunk["start"]:chunk["end"]]
    # access control still applies to chunk search
    assert store.search(w.conn, w.carol, "Postgres replicas Frankfurt datacenter", embedder=HashEmbedder()) == []


def test_chunks_from_old_content_are_ignored_until_rechunked(world):
    w = world
    i = save(w, "Notes", "The launch is planned for Lisbon in spring.", key="notes.md")
    run_until_idle(w.ctx)
    save(w, "Notes", "The launch moved to Oslo in autumn.", key="notes.md")
    # before the worker runs, stale chunks don't surface a snippet of text that no longer exists
    hits = store.search(w.conn, w.alice, "Lisbon spring launch", embedder=HashEmbedder())
    assert all(h["chunk"] is None for h in hits)
    run_until_idle(w.ctx)
    hit = store.search(w.conn, w.alice, "Oslo autumn launch", embedder=HashEmbedder())[0]
    assert hit["id"] == i and "Oslo" in hit["chunk"]["text"]


def test_metadata_only_versions_keep_chunks_valid_and_hygiene_backfills(world):
    w = world
    i = save(w, "Runbook", "Restart the billing worker with systemctl when queues back up.")
    run_until_idle(w.ctx)  # enrichment adds metadata-only versions after chunking
    hit = store.search(w.conn, w.alice, "restart billing worker", embedder=HashEmbedder())[0]
    assert hit["id"] == i and hit["chunk"]["no"] == 0
    # items without chunks (e.g. enriched before the migration) are re-queued by hygiene
    w.conn.execute("delete from item_chunks")
    from db4ai.maintainers import hygiene
    with w.conn.transaction():
        assert hygiene(w.ctx, {})(w.conn)["reembed"] == 1
    run_until_idle(w.ctx)
    assert w.conn.execute("select count(*) n from item_chunks where item_id=%s", (i,)).fetchone()["n"] == 1
