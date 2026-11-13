import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from db4ai import ingest, store
from db4ai.api import create_app
from db4ai.auth import AccessDenied
from db4ai.datasets import run_readonly_sql


def write_vault(root):
    (root / "people").mkdir(parents=True)
    (root / "Onboarding.md").write_text(
        "---\ntitle: Onboarding guide\ntags: [process]\nowner: dana\n---\n# Onboarding\n"
        "New hires pair with a buddy. See [[Buddy program]] and [[people/Dana|Dana]]. #hr\n")
    (root / "Buddy program.md").write_text("Buddies meet weekly for the first month.\n")
    (root / "people" / "Dana.md").write_text("Dana runs onboarding.\n")
    (root / ".obsidian").mkdir()
    (root / ".obsidian" / "app.json").write_text("{}")


def test_vault_import_is_idempotent_and_links(world, tmp_path):
    w = world
    vault = tmp_path / "vault"
    write_vault(vault)
    r = ingest.import_vault(w.conn, w.alice, w.eng, vault, blobs=w.blobs)
    assert (r["files"], r["wikilinks"]) == (3, 2)
    item = store.search(w.conn, w.alice, "buddy onboarding", types=["memory"])[0]
    cur = store.get_item(w.conn, w.alice, item["id"])
    assert cur["title"] == "Onboarding guide" and cur["metadata"]["owner"] == "dana"
    assert cur["metadata"]["tags"] == ["hr", "process"] and cur["metadata"]["path"] == "Onboarding.md"

    again = ingest.import_vault(w.conn, w.alice, w.eng, vault, blobs=w.blobs)
    assert again["changed"] == 0                         # nothing changed -> no new versions
    (vault / "Buddy program.md").write_text("Buddies meet twice a week for the first month.\n")
    third = ingest.import_vault(w.conn, w.alice, w.eng, vault, blobs=w.blobs)
    assert third["changed"] == 1


def test_csv_becomes_bi_table(world, tmp_path):
    w = world
    f = tmp_path / "Revenue 2026.csv"
    f.write_text("month,region,revenue,closed\n2026-01-01,EU,1200.5,true\n2026-02-01,US,900,false\n")
    r = ingest.import_file(w.conn, w.alice, w.eng, f, blobs=w.blobs)
    assert r == {"dataset": "Revenue 2026", "table": "data.eng_revenue_2026", "rows": 2}
    cols = {c["column_name"]: c["data_type"] for c in w.conn.execute(
        "select column_name, data_type from information_schema.columns where table_schema='data' and table_name='eng_revenue_2026'")}
    assert cols["revenue"] == "double precision" and cols["closed"] == "boolean" and cols["month"].startswith("timestamp")
    log = w.conn.execute("select actor_id, source from change_log where entity='data.eng_revenue_2026'").fetchall()
    assert len(log) == 2 and log[0]["actor_id"] == w.alice.id
    assert run_readonly_sql(w.conn, w.root, "select sum(revenue) s from data.eng_revenue_2026")[0]["s"] == 2100.5
    with pytest.raises(AccessDenied):
        run_readonly_sql(w.conn, w.alice, "select 1")
    with pytest.raises(Exception):
        run_readonly_sql(w.conn, w.root, "delete from data.eng_revenue_2026")


def test_docx_import_stores_blob(world, tmp_path):
    import docx

    w = world
    d = docx.Document()
    d.add_paragraph("Board deck notes: Q4 hiring plan approved.")
    path = tmp_path / "board.docx"
    d.save(path)
    r = ingest.import_file(w.conn, w.alice, w.eng, path, blobs=w.blobs)
    cur = store.get_item(w.conn, w.alice, r["item_id"])
    assert "hiring plan" in cur["body"] and w.blobs.exists(cur["blob_sha256"])


def test_http_api_and_harness_page(world, db_url):
    w = world
    with TestClient(create_app(db_url)) as client:
        assert client.post("/items", json={"type": "memory", "title": "x"}).status_code == 401
        h = {"Authorization": f"Bearer {w.tokens['alice']}"}
        r = client.post("/items", headers=h, json={"type": "decision", "title": "Use Postgres",
                                                     "body": "Postgres is the only system of record.", "team": "eng"})
        assert r.status_code == 200
        item_id = r.json()["item_id"]
        assert client.get(f"/items/{item_id}", headers=h).json()["title"] == "Use Postgres"
        assert client.get("/search", params={"q": "system of record"}, headers=h).json()[0]["id"] == item_id
        carol = {"Authorization": f"Bearer {w.tokens['carol']}"}
        assert client.get(f"/items/{item_id}", headers=carol).status_code == 403

        # queue an ask_first decision and approve it through the web page
        from db4ai import harness
        from db4ai.auth import maintainer_agent
        with w.conn.transaction():
            d = harness.propose(w.conn, maintainer_agent(w.conn), "archive_item", {"item_id": item_id}, reason="unused")
        assert client.get("/harness").status_code == 200 and "Sign in" in client.get("/harness").text
        client.post("/login", data={"token": w.tokens["alice"]})
        page = client.get("/harness").text
        assert "archive_item" in page and "Approve" in page
        client.post(f"/harness/{d['decision_id']}/form", data={"verb": "approve", "note": "ok"})
        assert client.get(f"/items/{item_id}", headers=h).json()["status"] == "archived"
        hist = client.get(f"/items/{item_id}/history", headers=h).json()
        assert hist[-1]["harness_decision_id"] == str(d["decision_id"])


def test_mcp_tools_round_trip(world, monkeypatch):
    w = world
    monkeypatch.setenv("DB4AI_TOKEN", w.tokens["bob"])
    from db4ai import mcp_server

    mcp_server._conn = None

    async def call(name, args):
        res = await mcp_server.mcp.call_tool(name, args)
        return res

    tools = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert {"save_meeting", "remember", "search", "harness_decide", "history", "revert"} <= tools
    saved = mcp_server.save_meeting("Sprint planning", "We picked the billing epic.", attendees=["bob", "alice"])
    assert saved["changed"] and saved["version"] == 1
    hits = mcp_server.search("billing epic")
    assert hits[0]["id"] == saved["item_id"]
    hist = mcp_server.history(saved["item_id"])
    assert hist[0]["actor"] == "bob" and hist[0]["source"] == "claude-mcp"
    asyncio.run(call("remember", {"fact": "Deploys freeze on Fridays."}))
    assert mcp_server.search("deploys freeze")[0]["type"] == "memory"
    mcp_server._conn.close()
