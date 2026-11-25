import json

import httpx
import pytest

from db4ai import connectors, store
from db4ai.cli import main
from db4ai.notion import MAX_BLOCKS, Notion, NotionError, page_title, prop_text


def rt(s):
    return [{"type": "text", "plain_text": s}]


def blk(bid, typ, text="", children=None, **extra):
    return {"id": bid, "type": typ, "has_children": bool(children), typ: {"rich_text": rt(text), **extra},
            "_children": children or []}


class FakeNotion:
    """Just enough of the Notion API: POST /search (pages, paged) and GET /blocks/{id}/children (paged)."""

    def __init__(self, page_size=100):
        self.pages: dict[str, dict] = {}
        self.dbs: dict[str, dict] = {}
        self.blocks: dict[str, list[dict]] = {}  # parent id -> child blocks
        self.page_size = page_size
        self.fail_next: list[int] = []
        self.blocked: set[str] = set()
        self.requests: list[httpx.Request] = []

    def add(self, pid, title, blocks, edited="2026-11-01T10:00:00.000Z", parent=None, title_prop="title"):
        self.pages[pid] = {"object": "page", "id": pid, "created_time": "2026-10-01T00:00:00.000Z",
                           "last_edited_time": edited, "archived": False, "in_trash": False,
                           "url": f"https://www.notion.so/{pid}",
                           "parent": parent or {"type": "workspace", "workspace": True},
                           "properties": {title_prop: {"type": "title", "title": rt(title)}}}
        self._put(pid, blocks)

    def db(self, did, title, props, edited="2026-11-01T10:00:00.000Z"):
        """props: name -> type. Rows are pages added with parent database `did` and `values`."""
        self.dbs[did] = {"object": "database", "id": did, "last_edited_time": edited, "archived": False,
                         "in_trash": False, "title": rt(title),
                         "properties": {n: {"id": n[:4], "name": n, "type": t} for n, t in props.items()}}

    def row(self, did, pid, values, edited="2026-11-01T10:00:00.000Z", created="2026-10-01T00:00:00.000Z"):
        """values: name -> property value object (with "type")."""
        self.add(pid, "", [], edited=edited, parent={"type": "database_id", "database_id": did})
        self.pages[pid]["properties"] = values
        self.pages[pid]["created_time"] = created

    def _put(self, parent, blocks):
        self.blocks[parent] = blocks
        for b in blocks:
            self._put(b["id"], b.pop("_children", []))

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        assert req.headers["notion-version"] == "2022-06-28"
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0), headers={"retry-after": "1"},
                                  json={"code": "rate_limited", "message": "slow down"})
        if req.headers.get("authorization") != "Bearer secret":
            return httpx.Response(401, json={"code": "unauthorized", "message": "API token is invalid."})
        if req.url.path == "/v1/search":
            body = json.loads(req.content)
            kind = body["filter"]["value"]
            assert body["filter"]["property"] == "object" and kind in ("page", "database")
            objs = self.pages if kind == "page" else self.dbs
            return self._paged(req, [objs[i] for i in sorted(p for p in objs if not objs[p]["in_trash"])], body)
        if req.url.path.startswith("/v1/databases/"):
            did = req.url.path.split("/")[3]
            if did in self.blocked or did not in self.dbs:
                return httpx.Response(404, json={"code": "object_not_found", "message": "no"})
            rows = [self.pages[p] for p in sorted(self.pages, reverse=True)  # API order isn't ours to rely on
                    if (self.pages[p]["parent"].get("database_id") == did and not self.pages[p]["in_trash"])]
            return self._paged(req, rows, json.loads(req.content))
        bid = req.url.path.split("/")[3]
        if bid in self.blocked or bid not in self.blocks:
            return httpx.Response(404, json={"code": "object_not_found", "message": "no"})
        kids = self.blocks[bid]
        start = int(req.url.params.get("start_cursor") or 0)
        page = kids[start:start + self.page_size]
        more = start + self.page_size < len(kids)
        return httpx.Response(200, json={"results": page, "has_more": more,
                                         "next_cursor": str(start + self.page_size) if more else None})


    def _paged(self, req, objs, body):
        start = int(body.get("start_cursor") or 0)
        page = objs[start:start + self.page_size]
        more = start + self.page_size < len(objs)
        return httpx.Response(200, json={"results": page, "has_more": more,
                                         "next_cursor": str(start + self.page_size) if more else None})


def notion(fake, token="secret", **kw):
    return Notion(token, httpx.Client(transport=httpx.MockTransport(fake.handler)), sleep=lambda s: None, **kw)


def test_pages_sync_incrementally(world):
    w = world
    fake = FakeNotion(page_size=2)
    fake.add("p1", "Pricing plan", [
        blk("b1", "heading_1", "Pricing"),
        blk("b2", "paragraph", "We ship the pricing page on Friday."),
        blk("b3", "bulleted_list_item", "Legal reviewed it", children=[blk("b4", "paragraph", "on Monday")]),
        blk("b5", "to_do", "Tell sales", checked=True),
        blk("b6", "code", "print(1)", language="python"),
        blk("b7", "child_page", title="Sub page"),
        blk("b8", "divider"),
    ])
    fake.add("p2", "Hiring", [blk("c1", "paragraph", "Two backend roles open.")], edited="2026-11-02T10:00:00.000Z")
    fake.add("p3", "Row", [blk("r1", "quote", "a database row body")], edited="2026-11-03T10:00:00.000Z",
             parent={"type": "database_id", "database_id": "db1"}, title_prop="Name")

    r = connectors.run(w.conn, w.bob, "notion", w.eng, notion(fake, batch_size=2))
    assert (r["batches"], r["saved"], r["changed"]) == (2, 3, 3)
    assert r["cursor"].startswith("v2:rows:") and r["cursor"].endswith(":edited:2026-11-03T10:00:00.000Z")
    hit = store.search(w.conn, w.alice, "pricing page Friday")[0]
    item = store.get_item(w.conn, w.alice, hit["id"])
    assert item["title"] == "Pricing plan" and item["external_key"] == "notion:p1"
    assert item["source"] == "connector:notion" and item["type"] == "document"
    assert item["body"] == ("# Pricing\nWe ship the pricing page on Friday.\n- Legal reviewed it\n  on Monday\n"
                            "- [x] Tell sales\n```python\nprint(1)\n```\n[Sub page]\n---")
    assert item["metadata"] == {"url": "https://www.notion.so/p1", "last_edited_time": "2026-11-01T10:00:00.000Z",
                                "created_time": "2026-10-01T00:00:00.000Z", "parent_type": "workspace"}
    row = store.get_item(w.conn, w.alice, store.search(w.conn, w.alice, "database row body")[0]["id"])
    assert row["title"] == "Row" and row["metadata"]["parent_id"] == "db1"

    # Idle: only the page at the cursor time is re-read, and it writes nothing new.
    idle = connectors.run(w.conn, w.bob, "notion", w.eng, notion(fake))
    assert (idle["saved"], idle["changed"]) == (1, 0)
    reads = [q.url.path for q in fake.requests[-3:]]
    assert "/v1/blocks/p1/children" not in reads

    # An edit is a new version; untouched older pages aren't read again.
    fake.add("p2", "Hiring", [blk("c1", "paragraph", "Three backend roles open.")], edited="2026-11-04T09:00:00.000Z")
    fake.requests.clear()
    r = connectors.run(w.conn, w.bob, "notion", w.eng, notion(fake))
    assert r["changed"] == 1 and r["cursor"].endswith(":edited:2026-11-04T09:00:00.000Z")
    assert not any(q.url.path == "/v1/blocks/p1/children" for q in fake.requests)
    p2 = store.get_item(w.conn, w.alice, store.search(w.conn, w.alice, "Three backend roles")[0]["id"])
    assert p2["version"] == 2

    # A cursor from another format starts a full listing (idempotent).
    connectors.apply_batch(w.conn, w.bob, "notion", w.eng, [], "old-format", r["cursor"])
    again = connectors.run(w.conn, w.bob, "notion", w.eng, notion(fake))
    assert (again["saved"], again["changed"]) == (3, 0)


def test_deletions_and_fail_closed(world):
    w = world
    fake = FakeNotion()
    fake.add("p1", "Keep", [blk("a", "paragraph", "keep me")])
    fake.add("p2", "Gone", [blk("b", "paragraph", "trash me")])
    connectors.run(w.conn, w.bob, "notion", w.eng, notion(fake))

    fake.pages["p2"]["in_trash"] = True
    r = connectors.run(w.conn, w.bob, "notion", w.eng, notion(fake))
    assert r["deleted"] == 1
    [d] = w.conn.execute("select payload, status from harness_decisions where action_type='archive_item'").fetchall()
    assert d["status"] == "pending" and d["payload"]["source_deleted"] == "notion"
    # Re-running doesn't propose it again.
    assert connectors.run(w.conn, w.bob, "notion", w.eng, notion(fake))["deleted"] == 0

    # The integration lost access to everything: no deletions proposed.
    fake.pages.clear()
    assert connectors.run(w.conn, w.bob, "notion", w.eng, notion(fake))["deleted"] == 0


def test_errors_retries_and_limits(world, monkeypatch):
    w = world
    fake = FakeNotion()
    fake.add("p1", "Readable", [blk("a", "paragraph", "fine")])
    fake.add("p2", "Unshared meanwhile", [blk("b", "paragraph", "secret")])
    fake.blocked.add("p2")
    fake.fail_next = [429, 503]
    slept = []
    n = Notion("secret", httpx.Client(transport=httpx.MockTransport(fake.handler)), sleep=slept.append)
    r = connectors.run(w.conn, w.bob, "notion", w.eng, n)
    assert r["saved"] == 1 and slept == [1.0, 1.0]  # Retry-After honoured; the unreadable page skipped

    with pytest.raises(NotionError, match="401"):
        connectors.run(w.conn, w.bob, "notion", w.eng, notion(fake, token="wrong"))

    # A page over the block cap is skipped, not half-synced.
    monkeypatch.setattr("db4ai.notion.MAX_BLOCKS", 3)
    fake.add("p3", "Huge", [blk(f"h{i}", "paragraph", f"line {i}") for i in range(4)],
             edited="2026-11-09T00:00:00.000Z")
    r = connectors.run(w.conn, w.bob, "notion", w.eng, notion(fake))
    assert r["changed"] == 0 and r["cursor"].endswith(":edited:2026-11-09T00:00:00.000Z")
    assert not store.search(w.conn, w.alice, "line 1")
    assert MAX_BLOCKS == 10_000

    assert page_title({"properties": {}}) == "Untitled"


def test_cli_notion_needs_token(db_url, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DB4AI_DATABASE_URL", db_url)
    monkeypatch.setenv("DB4AI_BLOB_URL", f"file://{tmp_path}/blobs")
    monkeypatch.delenv("DB4AI_NOTION_TOKEN", raising=False)
    main(["user", "dana", "--team", "eng:owner"])
    json.loads(capsys.readouterr().out)
    with pytest.raises(SystemExit, match="DB4AI_NOTION_TOKEN"):
        main(["sync", "notion", "--team", "eng", "--as", "dana"])


def P(t, v):
    return {"type": t, t: v}


def _tables(w):
    return {r["external_key"]: r for r in w.conn.execute(
        "select * from datasets where external_key like 'notion:%%'").fetchall()}


def _archives(w):
    return w.conn.execute("select payload from harness_decisions where action_type='archive_dataset'"
                          " and status='pending'").fetchall()


def test_databases_as_datasets(world, caplog):
    w = world
    fake = FakeNotion(page_size=2)
    fake.add("p1", "Notes", [blk("a", "paragraph", "plain page")], edited="2026-11-02T10:00:00.000Z")
    fake.db("db1", "Deals", {"Name": "title", "Amount": "number", "Stage": "status", "Tags": "multi_select",
                             "Close": "date", "Won": "checkbox", "Owner": "people"})
    fake.row("db1", "r1", {"Name": P("title", rt("Acme")), "Amount": P("number", 1200.0),
                           "Stage": P("status", {"name": "Open"}), "Tags": P("multi_select", [{"name": "a"}, {"name": "b"}]),
                           "Close": P("date", {"start": "2026-12-01"}), "Won": P("checkbox", False),
                           "Owner": P("people", [{"id": "u1", "name": "Ana"}])}, created="2026-10-01T00:00:00.000Z")
    fake.row("db1", "r2", {"Name": P("title", rt("Globex")), "Amount": P("number", None),
                           "Won": P("checkbox", True)}, created="2026-10-02T00:00:00.000Z")
    fake.db("db2", "", {"Title": "title"})  # untitled, no rows

    r = connectors.run(w.conn, w.bob, "notion", w.eng, notion(fake))
    assert r["datasets"] == 2 and r["saved"] == 3  # the plain page and both rows' bodies
    t = _tables(w)
    assert set(t) == {"notion:db1", "notion:db2"}
    table = f"data.{t['notion:db1']['table_name']}"
    assert table == "data.eng_deals"
    assert [c["name"] for c in t["notion:db1"]["columns"]] == \
        ["page_id", "name", "amount", "close", "owner", "stage", "tags", "won"]
    rows = w.conn.execute(f"select * from {table} order by _row_id").fetchall()
    assert [(x["page_id"], x["name"], x["amount"], x["stage"], x["tags"], x["won"], x["owner"]) for x in rows] == [
        ("r1", "Acme", 1200, "Open", "a, b", False, "Ana"), ("r2", "Globex", None, None, None, True, None)]
    assert t["notion:db2"]["table_name"].startswith("eng_notion_db2")
    log = [x["after"] for x in w.conn.execute(
        "select after from change_log where entity='sync_cursor' order by id").fetchall()]
    assert [x.get("datasets", 0) for x in log][:2] == [1, 1]  # databases first, one per batch
    assert log[0]["cursor"] is None

    # Idle: nothing re-read, no dataset batches.
    fake.requests.clear()
    idle = connectors.run(w.conn, w.bob, "notion", w.eng, notion(fake))
    assert idle["datasets"] == 0
    assert not any("/databases/" in q.url.path for q in fake.requests)

    # A row edit re-reads its database only.
    fake.row("db1", "r2", {"Name": P("title", rt("Globex")), "Amount": P("number", 50),
                           "Won": P("checkbox", True)}, edited="2026-11-05T00:00:00.000Z",
             created="2026-10-02T00:00:00.000Z")
    fake.requests.clear()
    r = connectors.run(w.conn, w.bob, "notion", w.eng, notion(fake))
    assert r["datasets"] == 1
    assert [q.url.path for q in fake.requests if "/databases/" in q.url.path] == ["/v1/databases/db1/query"]
    assert [x["amount"] for x in w.conn.execute(f"select amount from {table} order by page_id").fetchall()] \
        == [1200, 50]

    # A deleted row (no edit time to see) changes the rows hash: databases are re-read and the row goes.
    fake.pages["r1"]["in_trash"] = True
    r = connectors.run(w.conn, w.bob, "notion", w.eng, notion(fake))
    assert r["datasets"] == 2 and r["deleted"] == 1  # r1's body item is proposed for archiving too
    assert [x["page_id"] for x in w.conn.execute(f"select page_id from {table}").fetchall()] == ["r2"]

    # A new property is schema drift: proposed, and the snapshot waits.
    fake.db("db1", "Deals", {"Name": "title", "Amount": "number", "Stage": "status", "Tags": "multi_select",
                             "Close": "date", "Won": "checkbox", "Owner": "people", "Region": "select"},
            edited="2026-11-06T00:00:00.000Z")
    r = connectors.run(w.conn, w.bob, "notion", w.eng, notion(fake))
    [d] = w.conn.execute("select payload from harness_decisions where action_type='alter_dataset'"
                         " and status='pending'").fetchall()
    assert [a["name"] for a in d["payload"]["add"]] == ["region"]

    # An unreadable database is skipped, not deleted; an unshared one is proposed for archiving.
    fake.blocked.add("db1")
    del fake.dbs["db2"]
    r = connectors.run(w.conn, w.bob, "notion", w.eng, notion(fake))
    assert r["datasets_deleted"] == 1
    assert [a["payload"]["dataset_id"] for a in _archives(w)] == [str(t["notion:db2"]["id"])]
    assert "can't be read" in caplog.text

    # The integration lost everything: no dataset deletions.
    fake.pages.clear()
    fake.dbs.clear()
    assert connectors.run(w.conn, w.bob, "notion", w.eng, notion(fake))["datasets_deleted"] == 0


def test_v1_cursor_rereads_databases_once(world):
    w = world
    fake = FakeNotion()
    fake.add("p1", "Page", [blk("a", "paragraph", "hello")], edited="2026-11-03T10:00:00.000Z")
    fake.db("db1", "Team", {"Name": "title"})
    fake.row("db1", "r1", {"Name": P("title", rt("Ana"))})  # edited before the cursor: only the upgrade reads it
    connectors.apply_batch(w.conn, w.bob, "notion", w.eng, [], "v1:edited:2026-11-03T10:00:00.000Z", None)
    r = connectors.run(w.conn, w.bob, "notion", w.eng, notion(fake))
    assert r["datasets"] == 1 and r["saved"] == 1  # pages at the cursor time only
    assert r["cursor"].startswith("v2:rows:") and r["cursor"].endswith(":edited:2026-11-03T10:00:00.000Z")
    assert connectors.run(w.conn, w.bob, "notion", w.eng, notion(fake))["datasets"] == 0


def test_prop_text():
    assert prop_text(P("number", 3.5)) == "3.5" and prop_text(P("number", 4.0)) == "4"
    assert prop_text(P("date", {"start": "2026-01-01", "end": "2026-01-03"})) == "2026-01-01 → 2026-01-03"
    assert prop_text(P("formula", {"type": "number", "number": 7})) == "7"
    assert prop_text(P("formula", {"type": "string", "string": "x"})) == "x"
    assert prop_text(P("rollup", {"type": "array", "array": [P("title", rt("a")), P("number", 2)]})) == "a, 2"
    assert prop_text(P("rollup", {"type": "number", "number": 9})) == "9"
    assert prop_text(P("relation", [{"id": "x"}, {"id": "y"}])) == "x, y"
    assert prop_text(P("unique_id", {"prefix": "T", "number": 12})) == "T-12"
    assert prop_text(P("url", "https://a")) == "https://a" and prop_text(P("email", None)) == ""
    assert prop_text(P("created_by", {"id": "u9"})) == "u9" and prop_text(None) == ""
    assert prop_text(P("files", [{"name": "a.pdf"}])) == "a.pdf"
