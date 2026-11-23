import json
import re

import httpx
import pytest

from db4ai import connectors, store
from db4ai.cli import main
from db4ai.gdrive import FOLDER, GDOC, DriveError, GoogleDrive, TokenSource


class FakeDrive:
    """Just enough of the Drive v3 API: files.list (paged), changes (paged), export, media download, token refresh."""

    def __init__(self):
        self.files: dict[str, dict] = {}
        self.content: dict[str, str] = {}
        self.change_log: list[dict] = []   # change number i+1 = change_log[i]
        self.valid_tokens = {"good"}
        self.fail_next: list[int] = []     # status codes to return before serving normally
        self.requests: list[httpx.Request] = []
        self.blocked: set[str] = set()     # files whose export is forbidden

    def add(self, fid, name, mime, text, modified="2026-11-01T00:00:00Z", change=True, parent="root"):
        self.files[fid] = {"id": fid, "name": name, "mimeType": mime, "modifiedTime": modified,
                           "webViewLink": f"https://docs.google.com/d/{fid}", "trashed": False,
                           "size": str(len(text)), "parents": [parent]}
        self.content[fid] = text
        if change:
            self.change_log.append({"fileId": fid, "removed": False, "file": dict(self.files[fid])})

    def folder(self, fid, parent="root"):
        self.add(fid, fid, FOLDER, "", parent=parent)

    def move(self, fid, parent):
        self.files[fid]["parents"] = [parent]
        self.change_log.append({"fileId": fid, "removed": False, "file": dict(self.files[fid])})

    @staticmethod
    def matches(f, q):
        mimes = re.findall(r"mimeType = '([^']+)'", q)
        parents = re.findall(r"'([^']+)' in parents", q)
        return (not mimes or f["mimeType"] in mimes) and (not parents or bool(set(f["parents"]) & set(parents)))

    def remove(self, fid):
        self.files.pop(fid)
        self.change_log.append({"fileId": fid, "removed": True})

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.url.host == "oauth2.googleapis.com":
            form = dict(x.split("=") for x in req.content.decode().split("&"))
            if form.get("refresh_token") != "refresh-me":
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.valid_tokens.add("fresh")
            return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})
        if self.fail_next:
            code = self.fail_next.pop(0)
            return httpx.Response(code, json={"error": {"errors": [{"reason": "backendError"}]}})
        if req.headers.get("authorization", "").removeprefix("Bearer ") not in self.valid_tokens:
            return httpx.Response(401, json={"error": {"errors": [{"reason": "authError"}]}})
        path, q = req.url.path.removeprefix("/drive/v3"), req.url.params
        if path == "/changes/startPageToken":
            return httpx.Response(200, json={"startPageToken": str(len(self.change_log) + 1)})
        if path == "/files":
            ids = sorted(f for f in self.files
                         if not self.files[f]["trashed"] and self.matches(self.files[f], q.get("q", "")))
            size, start = int(q["pageSize"]), int(q.get("pageToken") or 0)
            page = ids[start:start + size]
            out = {"files": [self.files[i] for i in page]}
            if start + size < len(ids):
                out["nextPageToken"] = str(start + size)
            return httpx.Response(200, json=out)
        if path == "/changes":
            tok, size = int(q["pageToken"]), int(q["pageSize"])
            page = self.change_log[tok - 1:tok - 1 + size]
            out = {"changes": page}
            if tok - 1 + size < len(self.change_log):
                out["nextPageToken"] = str(tok + size)
            else:
                out["newStartPageToken"] = str(len(self.change_log) + 1)
            return httpx.Response(200, json=out)
        fid = path.split("/")[2]
        if path.endswith("/export"):
            if fid in self.blocked:
                return httpx.Response(403, json={"error": {"errors": [{"reason": "cannotExportFile"}]}})
            assert q["mimeType"] == "text/plain"
            return httpx.Response(200, content=("﻿" + self.content[fid]).encode())
        if q.get("alt") == "media":
            return httpx.Response(200, content=self.content[fid].encode())
        if fid in self.files:
            return httpx.Response(200, json=self.files[fid])
        return httpx.Response(404, json={"error": {"errors": [{"reason": "notFound"}]}})


def drive(fake, token="good", **kw):
    http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    return GoogleDrive(TokenSource(http, token), http, sleep=lambda s: None, **kw)


def eng_docs(w):
    return w.conn.execute("select external_key from items where external_key like 'gdrive:%' order by 1").fetchall()


def test_initial_listing_then_incremental_changes(world):
    w = world
    fake = FakeDrive()
    fake.add("d1", "Pricing plan", GDOC, "We ship the pricing page on Friday.\r\nLegal reviewed it.")
    fake.add("t1", "notes.md", "text/markdown", "# Notes\nbudget approved")
    fake.add("x1", "Budget", "application/vnd.google-apps.spreadsheet", "a,b")
    fake.add("d2", "Hiring", GDOC, "Two backend roles open.")

    r = connectors.run(w.conn, w.bob, "gdrive", w.eng, drive(fake, page_size=2))
    # Two list pages (the spreadsheet is skipped), then an empty changes page that writes nothing.
    assert r["saved"] == 3 and r["changed"] == 3
    assert r["cursor"] == "changes:5"
    hit = store.search(w.conn, w.alice, "pricing page Friday")[0]
    item = store.get_item(w.conn, w.alice, hit["id"])
    assert item["title"] == "Pricing plan" and item["body"] == "We ship the pricing page on Friday.\nLegal reviewed it."
    assert item["external_key"] == "gdrive:d1" and item["source"] == "connector:gdrive"
    assert item["metadata"] == {"mime_type": GDOC, "modified_time": "2026-11-01T00:00:00Z",
                                "web_view_link": "https://docs.google.com/d/d1"}
    log = w.conn.execute("select after from change_log where entity='sync_cursor' order by id").fetchall()
    assert [x["after"]["cursor"] for x in log] == ["list:5:2", "changes:5"]

    # Idle: nothing changed, nothing written.
    idle = connectors.run(w.conn, w.bob, "gdrive", w.eng, drive(fake))
    assert (idle["batches"], idle["saved"]) == (0, 0)

    # An edit, a new doc, a deletion and a trashed file.
    fake.add("d1", "Pricing plan", GDOC, "We ship the pricing page on Thursday.", modified="2026-11-02T00:00:00Z")
    fake.add("d3", "Offsite", GDOC, "Offsite is in Lisbon.")
    fake.remove("d2")
    fake.add("d4", "Old", GDOC, "trash me")
    fake.files["d4"]["trashed"] = True
    fake.change_log[-1]["file"]["trashed"] = True
    r = connectors.run(w.conn, w.bob, "gdrive", w.eng, drive(fake, page_size=3))
    assert (r["batches"], r["saved"], r["changed"], r["cursor"]) == (2, 2, 2, "changes:9")
    assert store.get_item(w.conn, w.alice, hit["id"])["version"] == 2
    assert [x["external_key"] for x in eng_docs(w)] == ["gdrive:d1", "gdrive:d2", "gdrive:d3", "gdrive:t1"]
    # The removed doc gets an ask_first archive proposal; the trashed one was never synced, so nothing to do.
    assert r["deleted"] == 1
    d2 = w.conn.execute("select id from items where external_key='gdrive:d2'").fetchone()["id"]
    [dec] = w.conn.execute("select payload, status from harness_decisions where action_type='archive_item'").fetchall()
    assert dec["payload"]["item_id"] == str(d2) and dec["status"] == "pending"
    assert store.get_item(w.conn, w.alice, d2)["status"] == "active"

    # Trashing a synced doc is a deletion too.
    fake.files["t1"]["trashed"] = True
    fake.change_log.append({"fileId": "t1", "removed": False, "file": dict(fake.files["t1"])})
    r = connectors.run(w.conn, w.bob, "gdrive", w.eng, drive(fake))
    assert (r["batches"], r["saved"], r["deleted"]) == (1, 0, 1)


def test_resume_mid_listing_and_errors(world):
    w = world
    fake = FakeDrive()
    for i in range(5):
        fake.add(f"d{i}", f"Doc {i}", GDOC, f"content {i}")
    fake.blocked.add("d1")
    # A crash after the first list page: the cursor is mid-listing and the next run resumes there.
    gen = drive(fake, page_size=2).fetch(None)
    first = next(gen)
    connectors.apply_batch(w.conn, w.bob, "gdrive", w.eng, first.records, first.cursor, None)
    assert first.cursor == "list:6:2" and [r.key for r in first.records] == ["d0"]  # d1 export forbidden: skipped
    r = connectors.run(w.conn, w.bob, "gdrive", w.eng, drive(fake, page_size=2))
    assert r["saved"] == 3 and r["cursor"] == "changes:6"
    assert len(eng_docs(w)) == 4

    # Transient errors are retried; a rejected token without refresh credentials is a DriveError.
    fake.fail_next = [503, 429]
    fake.add("d9", "Late", GDOC, "late doc")
    assert connectors.run(w.conn, w.bob, "gdrive", w.eng, drive(fake))["saved"] == 1
    with pytest.raises(DriveError, match="401"):
        connectors.run(w.conn, w.bob, "gdrive", w.eng, drive(fake, token="expired"))
    with pytest.raises(DriveError, match="not a Google Drive cursor"):
        list(drive(fake).fetch("123"))


def test_refresh_token(world):
    w = world
    fake = FakeDrive()
    fake.add("d1", "Doc", GDOC, "hello drive")
    http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    g = GoogleDrive(TokenSource(http, "expired", "refresh-me", "cid", "secret"), http, sleep=lambda s: None)
    assert connectors.run(w.conn, w.bob, "gdrive", w.eng, g)["saved"] == 1
    assert g.tokens.token == "fresh"
    assert any(r.url.host == "oauth2.googleapis.com" for r in fake.requests)
    # No token at all and no refresh credentials: refuse up front.
    with pytest.raises(DriveError, match="DB4AI_GDRIVE_TOKEN"):
        TokenSource(http)


def test_cli_gdrive_needs_token(db_url, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DB4AI_DATABASE_URL", db_url)
    monkeypatch.setenv("DB4AI_BLOB_URL", f"file://{tmp_path}/blobs")
    for k in ("TOKEN", "REFRESH_TOKEN", "CLIENT_ID", "CLIENT_SECRET"):
        monkeypatch.delenv(f"DB4AI_GDRIVE_{k}", raising=False)
    main(["user", "dana", "--team", "eng:owner"])
    json.loads(capsys.readouterr().out)
    with pytest.raises(SystemExit, match="DB4AI_GDRIVE_TOKEN"):
        main(["sync", "gdrive", "--team", "eng", "--as", "dana"])
    with pytest.raises(SystemExit, match="not a folder"):
        main(["sync", "transcripts", "--team", "eng", "--as", "dana"])


def test_folder_scope(world, monkeypatch):
    w = world
    fake = FakeDrive()
    fake.folder("eng")
    fake.folder("specs", parent="eng")
    fake.folder("hr")
    fake.add("a", "Roadmap", GDOC, "Roadmap for the API.", parent="eng")
    fake.add("b", "Spec", GDOC, "Spec of the sync engine.", parent="specs")
    fake.add("c", "Salaries", GDOC, "Confidential salaries.", parent="hr")
    fake.add("d", "Loose", GDOC, "A loose doc at the root.")

    monkeypatch.setenv("DB4AI_GDRIVE_FOLDERS", "eng")
    g = drive(fake, page_size=1)
    assert g.roots == ["eng"] and set(g.folders()) == {"eng", "specs"}
    r = connectors.run(w.conn, w.bob, "gdrive", w.eng, g)
    assert r["saved"] == 2 and r["cursor"] == g.scope_tag + "changes:8"
    assert [x["external_key"] for x in eng_docs(w)] == ["gdrive:a", "gdrive:b"]
    # The listing cursor names the folder chunk and page; resuming mid-listing works.
    log = w.conn.execute("select after from change_log where entity='sync_cursor' order by id").fetchall()
    assert log[0]["after"]["cursor"].startswith(g.scope_tag + "list:8:0:")
    idle = connectors.run(w.conn, w.bob, "gdrive", w.eng, drive(fake))
    assert (idle["batches"], idle["saved"], idle["deleted"]) == (0, 0, 0)

    # Changes outside the scope are ignored; a doc moved out of scope is a deletion; one moved in is synced.
    fake.add("c", "Salaries", GDOC, "Confidential salaries, 2027.", parent="hr", modified="2026-11-03T00:00:00Z")
    fake.move("a", "hr")
    fake.move("d", "specs")
    r = connectors.run(w.conn, w.bob, "gdrive", w.eng, drive(fake))
    assert (r["saved"], r["deleted"]) == (1, 1)
    assert [x["external_key"] for x in eng_docs(w)] == ["gdrive:a", "gdrive:b", "gdrive:d"]
    a = w.conn.execute("select id from items where external_key='gdrive:a'").fetchone()["id"]
    [dec] = w.conn.execute("select payload from harness_decisions where action_type='archive_item'").fetchall()
    assert dec["payload"]["item_id"] == str(a)

    # A whole folder moving in brings its existing files; one moving out makes its synced files deletions.
    fake.folder("ops", parent="hr")
    fake.add("e", "Runbook", GDOC, "Runbook for on-call.", parent="ops")
    fake.move("ops", "eng")
    fake.move("specs", "hr")  # b and d leave the scope with their folder; no change is reported for them
    r = connectors.run(w.conn, w.bob, "gdrive", w.eng, drive(fake))
    assert r["changed"] == 1 and r["deleted"] == 2
    assert "gdrive:e" in [x["external_key"] for x in eng_docs(w)]
    assert w.conn.execute("select count(*) n from harness_decisions where action_type='archive_item'"
                          " and status='pending'").fetchone()["n"] == 3
    # Re-running proposes nothing new.
    again = connectors.run(w.conn, w.bob, "gdrive", w.eng, drive(fake))
    assert (again["changed"], again["deleted"]) == (0, 0)

    # Changing the scope restarts with a full listing of the new scope.
    g2 = drive(fake, folders=["eng", "hr"])
    assert g2.scope_tag != g.scope_tag
    r = connectors.run(w.conn, w.bob, "gdrive", w.eng, g2)
    assert r["cursor"].startswith(g2.scope_tag + "changes:")
    assert "gdrive:c" in [x["external_key"] for x in eng_docs(w)]

    # Fail closed: a missing root or a file that isn't a folder is an error, not "everything was deleted".
    with pytest.raises(DriveError, match="can't be read"):
        connectors.run(w.conn, w.bob, "gdrive", w.eng, drive(fake, folders=["nope"]))
    with pytest.raises(DriveError, match="not a folder"):
        connectors.run(w.conn, w.bob, "gdrive", w.eng, drive(fake, folders=["e"]))
    with pytest.raises(DriveError, match="folder id"):
        drive(fake, folders=["x' or true"])
