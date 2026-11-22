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
