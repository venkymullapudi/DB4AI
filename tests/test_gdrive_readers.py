"""Drive permissions as readers: shared-with users become reader_emails, matched to actors' emails."""

import json

import httpx
import pytest

from db4ai import auth, connectors, harness, store
from db4ai.cli import main
from db4ai.gdrive import ACL_TAG, GDOC, TYPES_TAG as T

from test_gdrive import FakeDrive, drive


class PermDrive(FakeDrive):
    """FakeDrive plus permissions: in the listing (when asked for) and from permissions.list."""

    def __init__(self):
        super().__init__()
        self.perms: dict[str, list[dict]] = {}
        self.unlisted: set[str] = set()     # e.g. shared-drive items: no permissions in files.list
        self.perm_forbidden: set[str] = set()

    def share(self, fid, *perms, change=True):
        self.perms[fid] = [{"type": t, "role": "reader", **({"emailAddress": e} if e else {})} for t, e in perms]
        if change:
            self.change_log.append({"fileId": fid, "removed": False, "file": dict(self.files[fid])})

    def _with_perms(self, f, fields):
        f = dict(f)
        if "permissions(" in fields and f["id"] in self.perms and f["id"] not in self.unlisted:
            f["permissions"] = self.perms[f["id"]]
        return f

    def handler(self, req):
        path = req.url.path.removeprefix("/drive/v3")
        if path.endswith("/permissions"):
            self.requests.append(req)
            fid = path.split("/")[2]
            if fid in self.perm_forbidden:
                return httpx.Response(403, json={"error": {"errors": [{"reason": "insufficientFilePermissions"}]}})
            return httpx.Response(200, json={"permissions": self.perms.get(fid, [])})
        r = super().handler(req)
        fields = req.url.params.get("fields", "")
        if path in ("/files", "/changes") and r.status_code == 200:
            data = r.json()
            for k in ("files",):
                if k in data:
                    data[k] = [self._with_perms(f, fields) for f in data[k]]
            for c in data.get("changes", []):
                if "file" in c:
                    c["file"] = self._with_perms(self.files.get(c["fileId"], c["file"]), fields)
            return httpx.Response(200, json=data)
        return r


@pytest.fixture
def people(world):
    w = world
    with w.conn.transaction():
        dave = auth.create_actor(w.conn, "dave", email="Dave@Example.com")
        auth.add_member(w.conn, w.eng, dave, "member")
        auth.set_email(w.conn, "bob", "bob@example.com")
        auth.set_email(w.conn, "carol", "carol@example.com")   # sales: never an eng reader
    w.dave = auth.load_actor(w.conn, dave)
    return w


def items(w):
    return {r["external_key"].split(":", 1)[1]: r["id"] for r in w.conn.execute(
        "select id, external_key from items where external_key like 'gdrive:%'").fetchall()}


def names(w, iid):
    r = store.item_readers(w.conn, iid)
    return None if r is None else sorted(a["name"] for a in w.conn.execute(
        "select name from actors where id = any(%s)", (r,)).fetchall())


def test_drive_permissions_restrict_new_files(people):
    w = people
    fake = PermDrive()
    fake.add("d1", "Reorg", GDOC, "Confidential reorg plan")
    fake.share("d1", ("user", "dave@example.com"), ("user", "CAROL@example.com"), ("user", "x@other.com"),
               ("group", "eng@example.com"), change=False)
    fake.add("d2", "Handbook", GDOC, "Company handbook")
    fake.share("d2", ("user", "dave@example.com"), ("domain", None), change=False)
    fake.add("d3", "Shared drive doc", GDOC, "Shared drive notes")
    fake.share("d3", ("user", "bob@example.com"), change=False)
    fake.unlisted.add("d3")
    fake.add("d4", "Opaque", GDOC, "Viewer-only file")
    fake.unlisted.add("d4")
    fake.perm_forbidden.add("d4")

    r = connectors.run(w.conn, w.alice, "gdrive", w.eng, drive(fake, readers=True))
    assert r["cursor"].startswith(T + ACL_TAG)
    ids = items(w)
    assert names(w, ids["d1"]) == ["alice", "dave"]      # syncer stays; carol (sales), unknown email, group dropped
    assert names(w, ids["d2"]) is None                   # shared with the domain: the team reads it
    assert names(w, ids["d3"]) == ["alice", "bob"]       # permissions.list for files the listing leaves out
    assert names(w, ids["d4"]) is None                   # permissions unknown: access left as is
    with pytest.raises(KeyError):
        store.get_item(w.conn, w.bob, ids["d1"])
    listed = [q for q in fake.requests if q.url.path == "/drive/v3/files"]
    assert all("permissions(" in q.url.params["fields"] for q in listed)

    # A sharing change arrives through the changes feed and proposes set_item_access (ask_first).
    fake.share("d1", ("user", "dave@example.com"), ("user", "bob@example.com"))
    r = connectors.run(w.conn, w.alice, "gdrive", w.eng, drive(fake, readers=True))
    assert r["access_proposed"] == 1
    d = w.conn.execute("select * from harness_decisions where action_type='set_item_access'"
                       " and status='pending'").fetchone()
    assert names(w, ids["d1"]) == ["alice", "dave"]      # not applied until an owner approves
    harness.decide(w.conn, w.alice, d["id"], approve=True)
    assert names(w, ids["d1"]) == ["alice", "bob", "dave"]


def test_readers_off_by_default_and_turning_on_relists(people, monkeypatch):
    w = people
    monkeypatch.delenv("DB4AI_GDRIVE_READERS", raising=False)
    fake = PermDrive()
    fake.add("d1", "Reorg", GDOC, "Confidential reorg plan")
    fake.share("d1", ("user", "dave@example.com"), change=False)
    off = connectors.run(w.conn, w.alice, "gdrive", w.eng, drive(fake))
    assert ACL_TAG not in off["cursor"]
    iid = items(w)["d1"]
    assert names(w, iid) is None
    assert all("permissions" not in q.url.params.get("fields", "") for q in fake.requests)

    # Turning it on re-lists once: the existing file's readers differ, so a decision is proposed, nothing applied.
    monkeypatch.setenv("DB4AI_GDRIVE_READERS", "1")
    on = connectors.run(w.conn, w.alice, "gdrive", w.eng, drive(fake))
    assert on["cursor"].startswith(T + ACL_TAG) and on["access_proposed"] == 1 and on["changed"] == 0
    assert names(w, iid) is None
    assert connectors.run(w.conn, w.alice, "gdrive", w.eng, drive(fake))["batches"] == 0

    # Turning it off again re-lists once and changes no access.
    monkeypatch.setenv("DB4AI_GDRIVE_READERS", "0")
    back = connectors.run(w.conn, w.alice, "gdrive", w.eng, drive(fake))
    assert ACL_TAG not in back["cursor"] and back["access_proposed"] == 0


def test_reader_emails_on_sync_api(people):
    w = people
    out = connectors.apply_batch(w.conn, w.alice, "src", w.eng, [connectors.Record(
        key="k", type="document", title="T", body="Secret", readers=["bob"],
        reader_emails=["DAVE@example.com", "nobody@example.com"])], "c1", None)
    assert out["items"][0]["readers_dropped"] == ["nobody@example.com"]
    assert names(w, out["items"][0]["item_id"]) == ["alice", "bob", "dave"]


def test_actor_email_mapping(conn, db_url, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DB4AI_DATABASE_URL", db_url)
    monkeypatch.setenv("DB4AI_BLOB_URL", f"file://{tmp_path}/blobs")
    main(["user", "erin", "--team", "eng", "--email", "Erin@Example.com"])
    capsys.readouterr()
    assert conn.execute("select email from actors where name='erin'").fetchone()["email"] == "erin@example.com"
    main(["user", "frank"])
    capsys.readouterr()
    with pytest.raises(SystemExit, match="already mapped"):
        main(["email", "frank", "erin@EXAMPLE.com"])
    with pytest.raises(SystemExit, match="not an email"):
        main(["email", "frank", "frank"])
    with pytest.raises(SystemExit, match="no actor"):
        main(["email", "ghost", "g@example.com"])
    main(["email", "frank", "frank@example.com"])
    assert json.loads(capsys.readouterr().out)["email"] == "frank@example.com"
    main(["user", "frank"])   # re-running `user` without --email keeps the mapping
    capsys.readouterr()
    assert conn.execute("select email from actors where name='frank'").fetchone()["email"] == "frank@example.com"
    main(["email", "frank", "--clear"])
    capsys.readouterr()
    assert conn.execute("select email from actors where name='frank'").fetchone()["email"] is None
