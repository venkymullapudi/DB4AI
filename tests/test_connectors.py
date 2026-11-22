import json
import os

import pytest
from fastapi.testclient import TestClient

from db4ai import connectors, harness, service, store
from db4ai.api import create_app
from db4ai.auth import AccessDenied
from db4ai.cli import main
from db4ai.connectors import Batch, CursorConflict, Record, TranscriptFolder, parse_transcript

VTT = """WEBVTT
Kind: captions

NOTE recorded by the meeting bot
still the note

1
00:00:01.000 --> 00:00:04.500
<v Dana Smith>We ship the pricing page on Friday.</v>

2
00:00:04.500 --> 00:00:07.000
<v Dana Smith>Legal reviewed it.

3
00:01:02.250 --> 00:01:05.000
<v Lee>Then marketing sends the email Monday.</v>
"""

SRT = """1
00:00:00,000 --> 00:00:02,000
Kim: Welcome everyone.

2
00:00:02,000 --> 01:00:03,500
Kim: <i>Budget</i> is approved.
"""


def test_parse_vtt_srt_and_plain():
    body, meta = parse_transcript(VTT, ".vtt")
    assert body == ("Dana Smith: We ship the pricing page on Friday. Legal reviewed it.\n\n"
                    "Lee: Then marketing sends the email Monday.")
    assert meta == {"format": "vtt", "speakers": ["Dana Smith", "Lee"], "duration_seconds": 65.0}
    body, meta = parse_transcript(SRT, ".srt")
    assert body == "Kim: Welcome everyone. Budget is approved."
    assert meta["duration_seconds"] == 3603.5 and meta["speakers"] == ["Kim"]
    body, meta = parse_transcript("Ana: hi\nBo: hello\nno speaker here\n", ".txt")
    assert body == "Ana: hi\n\nBo: hello\n\nno speaker here" and meta["speakers"] == ["Ana", "Bo"]


def test_folder_sync_is_incremental_and_idempotent(world, tmp_path):
    w = world
    root = tmp_path / "rec"
    (root / "2026").mkdir(parents=True)
    (root / "2026" / "standup.vtt").write_text(VTT)
    (root / "kickoff.srt").write_text(SRT)
    (root / ".trash").mkdir()
    (root / ".trash" / "old.vtt").write_text(VTT)
    (root / "slides.pdf").write_bytes(b"%PDF")
    os.utime(root / "kickoff.srt", ns=(1_000_000_000, 1_000_000_000))
    os.utime(root / "2026" / "standup.vtt", ns=(2_000_000_000, 2_000_000_000))

    r = connectors.run(w.conn, w.bob, "transcripts", w.eng, TranscriptFolder(root, batch_size=1))
    assert (r["batches"], r["saved"], r["changed"], r["cursor"]) == (2, 2, 2, "2000000000")
    hit = store.search(w.conn, w.alice, "pricing page Friday", types=["meeting"])[0]
    item = store.get_item(w.conn, w.alice, hit["id"])
    assert item["title"] == "standup" and item["metadata"]["path"] == "2026/standup.vtt"
    assert item["external_key"] == "transcripts:2026/standup.vtt" and item["source"] == "connector:transcripts"
    cur = connectors.get_cursor(w.conn, w.bob, "transcripts", w.eng)
    assert (cur["cursor"], cur["batches"], cur["records"]) == ("2000000000", 2, 2)
    log = w.conn.execute("select after from change_log where entity='sync_cursor' order by id").fetchall()
    assert [x["after"]["cursor"] for x in log] == ["1000000000", "2000000000"]

    # Nothing new: only the file at the cursor's mtime is re-sent, and it adds no version.
    again = connectors.run(w.conn, w.bob, "transcripts", w.eng, TranscriptFolder(root))
    assert (again["saved"], again["changed"]) == (1, 0)

    (root / "2026" / "standup.vtt").write_text(VTT.replace("Friday", "Thursday"))
    os.utime(root / "2026" / "standup.vtt", ns=(3_000_000_000, 3_000_000_000))
    third = connectors.run(w.conn, w.bob, "transcripts", w.eng, TranscriptFolder(root))
    assert (third["saved"], third["changed"], third["cursor"]) == (1, 1, "3000000000")
    assert store.get_item(w.conn, w.alice, hit["id"])["version"] == 2


def test_cursor_compare_and_set_and_atomicity(world):
    w = world
    rec = Record(key="a", type="document", title="A", body="alpha")
    connectors.apply_batch(w.conn, w.bob, "drive", w.eng, [rec], "p1", None)
    with pytest.raises(CursorConflict):  # a second syncer still thinks the cursor is unset
        connectors.apply_batch(w.conn, w.bob, "drive", w.eng, [Record("b", "document", "B")], "p2", None)
    assert w.conn.execute("select count(*) n from items where external_key like 'drive:%'").fetchone()["n"] == 1
    # A failing record rolls back the whole batch, cursor included.
    with pytest.raises(ValueError):
        connectors.apply_batch(w.conn, w.bob, "drive", w.eng, [Record("c", "document", "C"), Record("", "document", "X")],
                               "p2", "p1")
    assert connectors.get_cursor(w.conn, w.bob, "drive", w.eng)["cursor"] == "p1"
    # Same key in another source is a different item.
    other = connectors.apply_batch(w.conn, w.bob, "notion", w.eng, [rec], "n1", None)
    first = w.conn.execute("select id from items where external_key='drive:a'").fetchone()["id"]
    assert other["items"][0]["item_id"] != first
    with pytest.raises(AccessDenied):
        connectors.apply_batch(w.conn, w.carol, "drive", w.eng, [rec], "p2", "p1")
    with pytest.raises(ValueError):
        connectors.get_cursor(w.conn, w.bob, "Bad Source!", w.eng)


def test_erased_item_is_not_resynced(world):
    w = world
    rec = Record(key="memo", type="document", title="Memo", body="Secret salary details for Dana.")
    r = connectors.apply_batch(w.conn, w.bob, "drive", w.eng, [rec], "p1", None)
    item_id = r["items"][0]["item_id"]
    d = service.request_erasure(w.conn, w.alice, str(item_id), "GDPR request")
    harness.decide(w.conn, w.root, d["decision_id"], True)
    again = connectors.apply_batch(w.conn, w.bob, "drive", w.eng, [rec], "p2", "p1")
    assert again["erased"] == 1 and again["changed"] == 0
    assert store.get_item(w.conn, w.root, item_id)["status"] == "erased"


def test_http_sync(world, db_url):
    w = world
    h = {"Authorization": f"Bearer {w.tokens['bob']}"}
    with TestClient(create_app(db_url)) as client:
        assert client.get("/sync/drive/cursor", headers=h).json()["cursor"] is None
        body = {"items": [{"key": "f1", "type": "document", "title": "Roadmap", "body": "Q3 goals"}],
                "cursor": "tok1", "expected_cursor": None}
        r = client.post("/sync/drive", json=body, headers=h)
        assert r.status_code == 200 and r.json()["changed"] == 1
        assert client.post("/sync/drive", json=body, headers=h).status_code == 409
        assert client.get("/sync/drive/cursor", headers=h).json()["cursor"] == "tok1"
        carol = {"Authorization": f"Bearer {w.tokens['carol']}"}
        assert client.post("/sync/drive", json={**body, "team": "eng"}, headers=carol).status_code == 403
        assert client.post("/sync/BAD!", json=body, headers=h).status_code == 400


def test_cli_sync(db_url, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DB4AI_DATABASE_URL", db_url)
    monkeypatch.setenv("DB4AI_BLOB_URL", f"file://{tmp_path}/blobs")
    main(["user", "dana", "--team", "eng:owner"])
    capsys.readouterr()
    (tmp_path / "rec").mkdir()
    (tmp_path / "rec" / "sync.vtt").write_text(VTT)
    main(["sync", "transcripts", str(tmp_path / "rec"), "--team", "eng", "--as", "dana"])
    out = json.loads(capsys.readouterr().out)
    assert out["saved"] == 1 and out["changed"] == 1 and out["source"] == "transcripts"


def _pending_archives(w):
    return w.conn.execute("select id, payload, status, proposed_by from harness_decisions"
                          " where action_type='archive_item' order by proposed_at").fetchall()


def test_source_deletion_proposes_archive_then_approve(world):
    w = world
    recs = [Record("a", "document", "A", "alpha text"), Record("b", "document", "B", "beta text")]
    r = connectors.apply_batch(w.conn, w.bob, "drive", w.eng, recs, "p1", None)
    a_id = r["items"][0]["item_id"]
    d = connectors.apply_batch(w.conn, w.bob, "drive", w.eng, [], "p2", "p1", deleted=["a", "nope"])
    assert d["deleted"] == 1 and d["deletions"][0]["status"] == "pending"
    # ask_first by default: nothing is archived yet; the proposal holds the item id only.
    assert store.get_item(w.conn, w.alice, a_id)["status"] == "active"
    [dec] = _pending_archives(w)
    assert dec["payload"] == {"item_id": str(a_id), "for_version": 1, "source_deleted": "drive"}
    assert dec["proposed_by"] == w.bob.id
    # Re-sending the deletion is idempotent.
    again = connectors.apply_batch(w.conn, w.bob, "drive", w.eng, [], "p3", "p2", deleted=["a"])
    assert again["deletions"][0]["decision_id"] == dec["id"] and len(_pending_archives(w)) == 1
    log = w.conn.execute("select after from change_log where entity='sync_cursor' order by id").fetchall()
    assert log[-1]["after"] == {"cursor": "p3", "records": 0, "deleted": 1}
    harness.decide(w.conn, w.alice, dec["id"], True)
    assert store.get_item(w.conn, w.alice, a_id)["status"] == "archived"
    # Archived (or unknown) keys get no new proposal; the file coming back doesn't revive it.
    connectors.apply_batch(w.conn, w.bob, "drive", w.eng, [], "p4", "p3", deleted=["a"])
    assert len(_pending_archives(w)) == 1
    connectors.apply_batch(w.conn, w.bob, "drive", w.eng, [recs[0]], "p5", "p4")
    assert store.get_item(w.conn, w.alice, a_id)["status"] == "archived"
    with pytest.raises(ValueError):
        connectors.apply_batch(w.conn, w.bob, "drive", w.eng, [recs[1]], "p6", "p5", deleted=["b"])
    with pytest.raises(ValueError):
        connectors.apply_batch(w.conn, w.bob, "drive", w.eng, [], "p6", "p5", deleted=[str(i) for i in range(501)])


def test_restored_record_withdraws_pending_deletion(world):
    w = world
    rec = Record("a", "document", "A", "alpha text")
    r = connectors.apply_batch(w.conn, w.bob, "drive", w.eng, [rec], "p1", None)
    connectors.apply_batch(w.conn, w.bob, "drive", w.eng, [], "p2", "p1", deleted=["a"])
    connectors.apply_batch(w.conn, w.bob, "drive", w.eng, [rec], "p3", "p2")
    [dec] = _pending_archives(w)
    assert dec["status"] == "expired"
    with pytest.raises(harness.HarnessError):
        harness.decide(w.conn, w.alice, dec["id"], True)
    assert store.get_item(w.conn, w.alice, r["items"][0]["item_id"])["status"] == "active"
    # Deleted again later: a fresh proposal (the withdrawn one doesn't block it).
    d = connectors.apply_batch(w.conn, w.bob, "drive", w.eng, [], "p4", "p3", deleted=["a"])
    assert d["deleted"] == 1 and d["deletions"][0]["status"] == "pending"


def test_rejected_deletion_is_not_reproposed(world):
    w = world
    connectors.apply_batch(w.conn, w.bob, "drive", w.eng, [Record("a", "document", "A", "x")], "p1", None)
    d = connectors.apply_batch(w.conn, w.bob, "drive", w.eng, [], "p2", "p1", deleted=["a"])
    harness.decide(w.conn, w.alice, d["deletions"][0]["decision_id"], False)
    again = connectors.apply_batch(w.conn, w.bob, "drive", w.eng, [], "p3", "p2", deleted=["a"])
    assert again["deletions"][0]["status"] == "rejected" and len(_pending_archives(w)) == 1


def test_folder_deletions_fail_closed(world, tmp_path):
    w = world
    root = tmp_path / "rec"
    root.mkdir()
    (root / "a.vtt").write_text(VTT)
    (root / "b.srt").write_text(SRT)
    connectors.run(w.conn, w.bob, "transcripts", w.eng, TranscriptFolder(root))
    (root / "a.vtt").unlink()
    r = connectors.run(w.conn, w.bob, "transcripts", w.eng, TranscriptFolder(root))
    assert r["deleted"] == 1
    [dec] = _pending_archives(w)
    a = w.conn.execute("select id from items where external_key='transcripts:a.vtt'").fetchone()["id"]
    assert dec["payload"]["item_id"] == str(a)
    # Pending already: an idle run writes nothing more.
    n = w.conn.execute("select count(*) n from change_log").fetchone()["n"]
    idle = connectors.run(w.conn, w.bob, "transcripts", w.eng, TranscriptFolder(root))
    assert idle["deleted"] == 0
    assert w.conn.execute("select count(*) n from change_log").fetchone()["n"] == n + 1  # b.srt re-sent, no version
    # An emptied (unmounted?) folder proposes nothing; a missing one is an error.
    (root / "b.srt").unlink()
    assert connectors.run(w.conn, w.bob, "transcripts", w.eng, TranscriptFolder(root))["deleted"] == 0
    with pytest.raises(FileNotFoundError):
        connectors.run(w.conn, w.bob, "transcripts", w.eng, TranscriptFolder(tmp_path / "gone"))


def test_http_sync_deleted(world, db_url):
    w = world
    h = {"Authorization": f"Bearer {w.tokens['bob']}"}
    with TestClient(create_app(db_url)) as client:
        body = {"items": [{"key": "f1", "type": "document", "title": "Roadmap", "body": "Q3 goals"}], "cursor": "t1"}
        assert client.post("/sync/drive", json=body, headers=h).status_code == 200
        r = client.post("/sync/drive", json={"deleted": ["f1"], "cursor": "t2", "expected_cursor": "t1"}, headers=h)
        assert r.status_code == 200 and r.json()["deleted"] == 1
        assert r.json()["deletions"][0]["status"] == "pending"
