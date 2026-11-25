import json

import httpx
import pytest

from db4ai import connectors, store
from db4ai.cli import main
from db4ai.miro import MAX_ITEMS, Miro, MiroError, html_text, is_private, item_line

OPEN = {"sharingPolicy": {"access": "private", "teamAccess": "edit", "organizationAccess": "private"}}
PRIVATE = {"sharingPolicy": {"access": "private", "teamAccess": "private", "organizationAccess": "private"}}


def it(iid, typ, x=0, y=0, parent=None, **data):
    i = {"id": iid, "type": typ, "data": data, "position": {"x": x, "y": y}}
    if parent:
        i["parent"] = {"id": parent}
    return i


class FakeMiro:
    """Just enough of Miro's REST API v2: GET /v2/boards (offset paging) and /v2/boards/{id}/items (cursor paging)."""

    def __init__(self, page_size=50):
        self.boards: dict[str, dict] = {}
        self.items: dict[str, list[dict]] = {}
        self.page_size = page_size
        self.fail_next: list[int] = []
        self.blocked: set[str] = set()
        self.requests: list[httpx.Request] = []

    def add(self, bid, name, items, modified="2026-11-01T10:00:00Z", policy=OPEN, team="t1", description=""):
        self.boards[bid] = {"id": bid, "type": "board", "name": name, "description": description,
                            "modifiedAt": modified, "createdAt": "2026-10-01T00:00:00Z",
                            "viewLink": f"https://miro.com/app/board/{bid}/", "team": {"id": team}, "policy": policy}
        self.items[bid] = items

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0), headers={"retry-after": "1"},
                                  json={"code": "tooManyRequests", "message": "slow down"})
        if req.headers.get("authorization") != "Bearer secret":
            return httpx.Response(401, json={"code": "tokenNotProvided", "message": "bad token"})
        parts = req.url.path.split("/")
        if req.url.path == "/v2/boards":
            team = req.url.params.get("team_id")
            boards = [self.boards[b] for b in sorted(self.boards) if not team or self.boards[b]["team"]["id"] == team]
            off, lim = int(req.url.params["offset"]), int(req.url.params["limit"])
            assert lim <= 50
            page = boards[off:off + min(lim, self.page_size)]
            return httpx.Response(200, json={"data": page, "total": len(boards), "size": len(page), "offset": off})
        assert parts[4] == "items"
        bid = parts[3]
        if bid in self.blocked or bid not in self.items:
            return httpx.Response(404, json={"code": "boardNotFound", "message": "no"})
        start = int(req.url.params.get("cursor") or 0)
        page = self.items[bid][start:start + self.page_size]
        more = start + self.page_size < len(self.items[bid])
        out = {"data": page, "size": len(page)}
        if more:
            out["cursor"] = str(start + self.page_size)
        return httpx.Response(200, json=out)


def miro(fake, token="secret", **kw):
    return Miro(token, httpx.Client(transport=httpx.MockTransport(fake.handler)), sleep=lambda s: None, **kw)


def test_boards_sync_incrementally(world):
    w = world
    fake = FakeMiro(page_size=2)
    fake.add("b1", "Q4 planning", [
        it("f2", "frame", x=0, y=500, title="Risks"),
        it("f1", "frame", x=0, y=0, title="Goals"),
        it("s1", "sticky_note", x=10, y=20, parent="f1", content="<p>Ship pricing page</p>"),
        it("s0", "sticky_note", x=5, y=10, parent="f1", content="<p>Hire two engineers</p><p>by March</p>"),
        it("c1", "card", x=0, y=10, parent="f2", title="Vendor delay", description="<p>Chips &amp; boards late</p>"),
        it("t1", "text", x=0, y=-100, content="<b>Owner:</b> Alice"),
        it("i1", "image", x=0, y=900, title="whiteboard photo"),
        it("i2", "image", x=0, y=901),
        it("e1", "embed", x=0, y=902, url="https://example.com/doc"),
        it("k1", "connector"),
    ], description="Planning board for Q4")
    fake.add("b2", "Retro", [it("s", "sticky_note", content="Too many meetings")], modified="2026-11-02T10:00:00Z")
    fake.add("b3", "Secret board", [it("s", "sticky_note", content="layoffs plan")], policy=PRIVATE,
             modified="2026-11-03T10:00:00Z")

    r = connectors.run(w.conn, w.bob, "miro", w.eng, miro(fake, batch_size=1))
    assert (r["batches"], r["saved"], r["changed"]) == (2, 2, 2)
    assert r["cursor"].startswith("v1:scope:") and r["cursor"].endswith(":modified:2026-11-02T10:00:00Z")
    assert not store.search(w.conn, w.alice, "layoffs plan")  # private boards never sync
    item = store.get_item(w.conn, w.alice, store.search(w.conn, w.alice, "pricing page")[0]["id"])
    assert item["title"] == "Q4 planning" and item["type"] == "whiteboard" and item["external_key"] == "miro:b1"
    assert item["source"] == "connector:miro"
    assert item["body"] == ("Planning board for Q4\nOwner: Alice\n[image: whiteboard photo]\n<https://example.com/doc>\n\n"
                            "## Goals\n- Hire two engineers by March\n- Ship pricing page\n\n"
                            "## Risks\n- Vendor delay: Chips & boards late")
    assert item["metadata"] == {"view_link": "https://miro.com/app/board/b1/", "modified_at": "2026-11-01T10:00:00Z",
                                "created_at": "2026-10-01T00:00:00Z", "team_id": "t1"}

    # Idle: only the board at the cursor time is re-read, and nothing new is written.
    fake.requests.clear()
    idle = connectors.run(w.conn, w.bob, "miro", w.eng, miro(fake))
    assert (idle["saved"], idle["changed"]) == (1, 0)
    assert not any(q.url.path == "/v2/boards/b1/items" for q in fake.requests)

    # An edit is a new version.
    fake.add("b1", "Q4 planning", [it("s", "sticky_note", content="Pricing page shipped")],
             modified="2026-11-05T00:00:00Z")
    r = connectors.run(w.conn, w.bob, "miro", w.eng, miro(fake))
    assert r["changed"] == 1 and r["cursor"].endswith(":modified:2026-11-05T00:00:00Z")
    assert store.get_item(w.conn, w.alice, item["id"])["version"] == 2

    # Another team scope re-lists every board in it.
    r = connectors.run(w.conn, w.bob, "miro", w.eng, miro(fake, team="t1"))
    assert (r["saved"], r["changed"]) == (2, 0)


def test_deletions_private_and_fail_closed(world):
    w = world
    fake = FakeMiro()
    fake.add("b1", "Keep", [it("a", "sticky_note", content="keep me")])
    fake.add("b2", "Gone", [it("b", "sticky_note", content="delete me")])
    fake.add("b3", "Going private", [it("c", "sticky_note", content="hide me")])
    assert connectors.run(w.conn, w.bob, "miro", w.eng, miro(fake))["saved"] == 3

    del fake.boards["b2"]
    fake.boards["b3"]["policy"] = PRIVATE
    r = connectors.run(w.conn, w.bob, "miro", w.eng, miro(fake))
    assert r["deleted"] == 2
    rows = w.conn.execute("select payload, status from harness_decisions where action_type='archive_item'").fetchall()
    assert {d["status"] for d in rows} == {"pending"} and {d["payload"]["source_deleted"] for d in rows} == {"miro"}
    assert connectors.run(w.conn, w.bob, "miro", w.eng, miro(fake))["deleted"] == 0  # not proposed twice

    # An unreadable board is skipped, not reported deleted.
    fake.blocked.add("b1")
    fake.boards["b1"]["modifiedAt"] = "2026-11-06T00:00:00Z"
    r = connectors.run(w.conn, w.bob, "miro", w.eng, miro(fake))
    assert (r["saved"], r["deleted"]) == (0, 0)

    # The token lost access to everything: no deletions proposed.
    fake.boards.clear()
    assert connectors.run(w.conn, w.bob, "miro", w.eng, miro(fake))["deleted"] == 0


def test_errors_retries_and_limits(world, monkeypatch):
    w = world
    fake = FakeMiro()
    fake.add("b1", "Readable", [it("a", "sticky_note", content="fine")])
    fake.fail_next = [429, 503]
    slept = []
    m = Miro("secret", httpx.Client(transport=httpx.MockTransport(fake.handler)), sleep=slept.append)
    assert connectors.run(w.conn, w.bob, "miro", w.eng, m)["saved"] == 1 and slept == [1.0, 1.0]

    with pytest.raises(MiroError, match="401"):
        connectors.run(w.conn, w.bob, "miro", w.eng, miro(fake, token="wrong"))

    monkeypatch.setattr("db4ai.miro.MAX_ITEMS", 3)
    fake.add("b2", "Huge", [it(f"h{i}", "sticky_note", content=f"line {i}") for i in range(4)],
             modified="2026-11-09T00:00:00Z")
    r = connectors.run(w.conn, w.bob, "miro", w.eng, miro(fake))
    assert r["changed"] == 0 and r["cursor"].endswith(":modified:2026-11-09T00:00:00Z")
    assert not store.search(w.conn, w.alice, "line 1")
    assert MAX_ITEMS == 10_000


def test_rendering_helpers():
    assert html_text("<p>a&nbsp;b</p><p></p><p>c<br/>d</p>") == "a b c d"
    assert html_text(None) == ""
    assert item_line(it("x", "card", title="Only title")) == "- Only title"
    assert item_line(it("x", "shape", content="")) is None
    assert item_line(it("x", "app_card", description="desc")) == "- desc"
    assert is_private({}) and is_private({"policy": PRIVATE})
    assert not is_private({"policy": {"sharingPolicy": {"access": "view"}}})


def test_cli_miro_needs_token(db_url, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DB4AI_DATABASE_URL", db_url)
    monkeypatch.setenv("DB4AI_BLOB_URL", f"file://{tmp_path}/blobs")
    monkeypatch.delenv("DB4AI_MIRO_TOKEN", raising=False)
    main(["user", "dana", "--team", "eng:owner"])
    json.loads(capsys.readouterr().out)
    with pytest.raises(SystemExit, match="DB4AI_MIRO_TOKEN"):
        main(["sync", "miro", "--team", "eng", "--as", "dana"])
