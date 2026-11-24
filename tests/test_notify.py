import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from db4ai import harness, notify, store, worker
from db4ai.auth import AccessDenied, maintainer_agent
from db4ai.db import Provenance, transaction

SECRET = "Project Nightingale budget is 4.2M"


def item(w):
    prov = Provenance(w.alice.id, "test")
    with transaction(w.conn, prov):
        return store.save_item(w.conn, w.alice, prov, team_id=w.eng, type="memory", title=SECRET, body=SECRET)["item_id"]


def propose(w, action, payload, confidence=None):
    with w.conn.transaction():
        return harness.propose(w.conn, maintainer_agent(w.conn), action, payload,
                               reason=f"because {SECRET}", confidence=confidence)


def notify_jobs(w, status="queued"):
    return w.conn.execute("select * from jobs where kind='notify' and status=%s", (status,)).fetchall()


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(notify, "post_json", lambda url, payload: out.append((url, payload)))
    monkeypatch.setenv("DB4AI_NOTIFY_DELAY_SECONDS", "0")
    return out


def drain(w):
    w.conn.execute("delete from jobs where kind <> 'notify'")  # only exercise notifications here
    return worker.run_until_idle(w.ctx)


def test_no_webhook_no_job(world, sent):
    i = item(world)
    propose(world, "archive_item", {"item_id": str(i)})
    assert notify_jobs(world) == []


def test_burst_is_one_message_without_content(world, sent, monkeypatch):
    w = world
    monkeypatch.setenv("DB4AI_NOTIFY_WEBHOOKS", "https://hooks.example/slack")
    monkeypatch.setenv("DB4AI_PUBLIC_URL", "https://db4ai.example/")
    a, b = item(w), item(w)
    propose(w, "add_tags", {"item_id": str(a), "value": ["x"]}, confidence=1.0)  # auto: no notification
    assert notify_jobs(w) == []
    d1 = propose(w, "archive_item", {"item_id": str(a)})["decision_id"]
    d2 = propose(w, "archive_item", {"item_id": str(b)})["decision_id"]
    assert len(notify_jobs(w)) == 1  # deduped per webhook
    drain(w)
    assert len(sent) == 1
    url, payload = sent[0]
    text = payload["text"]
    assert url == "https://hooks.example/slack"
    assert "2 harness decisions" in text and "2 × archive_item" in text and "team eng" in text
    assert str(d1) in text and str(d2) in text and "https://db4ai.example/harness" in text
    assert "Nightingale" not in json.dumps(payload)  # no titles, bodies, reasons or diffs
    rows = w.conn.execute("select decision_id from notifications").fetchall()
    assert {r["decision_id"] for r in rows} == {d1, d2}
    # A later proposal names only the new decision.
    d3 = propose(w, "archive_item", {"item_id": str(item(w))})["decision_id"]
    drain(w)
    assert len(sent) == 2 and str(d3) in sent[1][1]["text"] and str(d1) not in sent[1][1]["text"]


def test_decided_before_send_is_skipped(world, sent, monkeypatch):
    w = world
    monkeypatch.setenv("DB4AI_NOTIFY_WEBHOOKS", "https://hooks.example/slack")
    d = propose(w, "archive_item", {"item_id": str(item(w))})["decision_id"]
    harness.decide(w.conn, w.alice, d, approve=False)
    drain(w)
    assert sent == []
    assert notify_jobs(w, "done")


def test_failed_post_retries_and_records_nothing(world, monkeypatch):
    w = world
    monkeypatch.setenv("DB4AI_NOTIFY_WEBHOOKS", "https://hooks.example/slack")
    monkeypatch.setenv("DB4AI_NOTIFY_DELAY_SECONDS", "0")
    calls = []

    def flaky(url, payload):
        calls.append(payload)
        if len(calls) == 1:
            raise RuntimeError("503")
    monkeypatch.setattr(notify, "post_json", flaky)
    propose(w, "archive_item", {"item_id": str(item(w))})
    drain(w)
    job = notify_jobs(w)[0]
    assert "503" in job["last_error"]
    assert w.conn.execute("select count(*) n from notifications").fetchone()["n"] == 0
    w.conn.execute("update jobs set run_after = now() where id = %s", (job["id"],))
    drain(w)
    assert len(calls) == 2
    assert w.conn.execute("select count(*) n from notifications").fetchone()["n"] == 1


def test_removed_webhook_is_a_noop(world, sent, monkeypatch):
    w = world
    monkeypatch.setenv("DB4AI_NOTIFY_WEBHOOKS", "https://hooks.example/old")
    propose(w, "archive_item", {"item_id": str(item(w))})
    monkeypatch.setenv("DB4AI_NOTIFY_WEBHOOKS", "https://hooks.example/new")
    drain(w)
    assert sent == [] and notify_jobs(w, "done")


def test_each_webhook_gets_its_own_job(world, sent, monkeypatch):
    w = world
    monkeypatch.setenv("DB4AI_NOTIFY_WEBHOOKS", "https://a.example/x, teams:https://b.example/y")
    propose(w, "archive_item", {"item_id": str(item(w))})
    assert len(notify_jobs(w)) == 2
    drain(w)
    by_url = dict(sent)
    assert "text" in by_url["https://a.example/x"]
    card = by_url["https://b.example/y"]["attachments"][0]
    assert card["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert "archive_item" in card["content"]["body"][0]["text"]


def test_message_caps_listed_decisions():
    ds = [{"id": f"id{i}", "action_type": "archive_item", "confidence": None, "expires_at": None,
           "team": None, "proposed_by": "db4ai-maintainer"} for i in range(25)]
    title, lines = notify.message(ds, None)
    assert "25 harness decisions" in title and lines[-2] == "…and 5 more" and len(lines) == notify.MAX_LISTED + 2


def test_real_http_post(world, monkeypatch):
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        w = world
        monkeypatch.setenv("DB4AI_NOTIFY_WEBHOOKS", f"http://127.0.0.1:{srv.server_port}/hook")
        monkeypatch.setenv("DB4AI_NOTIFY_DELAY_SECONDS", "0")
        propose(w, "archive_item", {"item_id": str(item(w))})
        drain(w)
        assert len(got) == 1 and "archive_item" in got[0]["text"]
    finally:
        srv.shutdown()


# ------------------------------------------------------------ per-team routing

def team_item(w, team):
    prov = Provenance(w.root.id, "test")
    with transaction(w.conn, prov):
        return store.save_item(w.conn, w.root, prov, team_id=team, type="memory", title="t", body="b")["item_id"]


def test_team_route_gets_only_its_team(world, sent, monkeypatch):
    w = world
    monkeypatch.setenv("DB4AI_NOTIFY_WEBHOOKS", "https://hooks.example/admins")
    monkeypatch.setenv("DB4AI_WEBHOOK_ENG", "teams:https://hooks.example/eng")
    monkeypatch.setenv("DB4AI_WEBHOOK_SALES", "https://hooks.example/sales")
    notify.add_route(w.conn, w.alice, w.eng, "DB4AI_WEBHOOK_ENG")      # an owner routes their own team
    notify.add_route(w.conn, w.root, w.sales, "DB4AI_WEBHOOK_SALES")   # an admin routes any team
    de = propose(w, "archive_item", {"item_id": str(team_item(w, w.eng))})["decision_id"]
    ds = propose(w, "archive_item", {"item_id": str(team_item(w, w.sales))})["decision_id"]
    assert len(notify_jobs(w)) == 3
    drain(w)
    by_url = {url: json.dumps(p) for url, p in sent}
    assert set(by_url) == {"https://hooks.example/admins", "https://hooks.example/eng", "https://hooks.example/sales"}
    assert str(de) in by_url["https://hooks.example/admins"] and str(ds) in by_url["https://hooks.example/admins"]
    assert str(de) in by_url["https://hooks.example/eng"] and str(ds) not in by_url["https://hooks.example/eng"]
    assert "AdaptiveCard" in by_url["https://hooks.example/eng"]
    assert str(ds) in by_url["https://hooks.example/sales"] and str(de) not in by_url["https://hooks.example/sales"]


def test_admin_only_actions_skip_team_routes(world, sent, monkeypatch):
    from db4ai import erasure
    w = world
    monkeypatch.setenv("DB4AI_WEBHOOK_ENG", "https://hooks.example/eng")
    notify.add_route(w.conn, w.alice, w.eng, "DB4AI_WEBHOOK_ENG")
    i = team_item(w, w.eng)
    with w.conn.transaction():
        erasure.request(w.conn, w.alice, i, reason="GDPR request")
    assert notify_jobs(w) == []  # no global webhook, and the team channel never hears about erasures
    monkeypatch.setenv("DB4AI_NOTIFY_WEBHOOKS", "https://hooks.example/admins")
    propose(w, "archive_item", {"item_id": str(i)})
    drain(w)
    eng = [p for url, p in sent if url.endswith("/eng")]
    admins = [p for url, p in sent if url.endswith("/admins")]
    assert "erase_item" not in json.dumps(eng) and "archive_item" in json.dumps(eng)
    assert "erase_item" in json.dumps(admins)


def test_same_url_global_and_routed_is_one_message(world, sent, monkeypatch):
    w = world
    monkeypatch.setenv("DB4AI_NOTIFY_WEBHOOKS", "https://hooks.example/one")
    monkeypatch.setenv("DB4AI_WEBHOOK_ENG", "https://hooks.example/one")
    notify.add_route(w.conn, w.alice, w.eng, "DB4AI_WEBHOOK_ENG")
    propose(w, "archive_item", {"item_id": str(team_item(w, w.eng))})
    drain(w)
    assert len(sent) == 1


def test_unset_env_var_and_removed_route(world, sent, monkeypatch, caplog):
    w = world
    notify.add_route(w.conn, w.alice, w.eng, "DB4AI_WEBHOOK_ENG")
    assert notify.routes(w.conn, w.bob, w.eng) == [
        {"team_id": w.eng, "env_name": "DB4AI_WEBHOOK_ENG", "configured": False}]
    propose(w, "archive_item", {"item_id": str(team_item(w, w.eng))})
    assert notify_jobs(w) == [] and "DB4AI_WEBHOOK_ENG is not set" in caplog.text
    monkeypatch.setenv("DB4AI_WEBHOOK_ENG", "https://hooks.example/eng")
    propose(w, "archive_item", {"item_id": str(team_item(w, w.eng))})
    assert len(notify_jobs(w)) == 1
    assert notify.remove_route(w.conn, w.alice, w.eng, "DB4AI_WEBHOOK_ENG")
    drain(w)  # route removed before the job ran: nothing sent
    assert sent == []
    log = w.conn.execute("select op, before, after from change_log where entity='notify_route' order by id").fetchall()
    assert [r["op"] for r in log] == ["add", "remove"]
    assert "hooks.example" not in json.dumps([dict(r) for r in log], default=str)  # never the URL


def test_route_access_and_validation(world):
    w = world
    with pytest.raises(AccessDenied):
        notify.add_route(w.conn, w.bob, w.eng, "DB4AI_WEBHOOK_ENG")     # member, not owner
    with pytest.raises(AccessDenied):
        notify.add_route(w.conn, w.alice, w.sales, "DB4AI_WEBHOOK_X")   # owner of another team
    with pytest.raises(AccessDenied):
        notify.routes(w.conn, w.carol, w.eng)
    for bad in ("DB4AI_GDRIVE_TOKEN", "DB4AI_WEBHOOK_", "db4ai_webhook_eng", "DB4AI_WEBHOOK_A-B"):
        with pytest.raises(ValueError):
            notify.add_route(w.conn, w.alice, w.eng, bad)
    notify.add_route(w.conn, w.alice, w.eng, "DB4AI_WEBHOOK_ENG")
    notify.add_route(w.conn, w.alice, w.eng, "DB4AI_WEBHOOK_ENG")  # idempotent, logged once
    assert w.conn.execute("select count(*) n from change_log where entity='notify_route'").fetchone()["n"] == 1
    assert not notify.remove_route(w.conn, w.alice, w.eng, "DB4AI_WEBHOOK_OTHER")


def test_routes_http(db_url, world, monkeypatch):
    from fastapi.testclient import TestClient

    from db4ai.api import create_app
    w = world
    monkeypatch.setenv("DB4AI_WEBHOOK_ENG", "https://hooks.example/secret-path")
    h = {"authorization": f"Bearer {w.tokens['alice']}"}
    with TestClient(create_app(db_url)) as client:
        r = client.put("/teams/eng/notify-routes/DB4AI_WEBHOOK_ENG", headers=h)
        assert r.status_code == 200 and r.json()["configured"] is True
        got = client.get("/teams/eng/notify-routes", headers=h)
        assert got.json()[0]["env_name"] == "DB4AI_WEBHOOK_ENG" and "secret-path" not in got.text
        bob = {"authorization": f"Bearer {w.tokens['bob']}"}
        assert client.put("/teams/eng/notify-routes/DB4AI_WEBHOOK_X", headers=bob).status_code == 403
        assert client.put("/teams/eng/notify-routes/HOME", headers=h).status_code == 400
        assert client.delete("/teams/eng/notify-routes/DB4AI_WEBHOOK_ENG", headers=h).status_code == 200
        assert client.delete("/teams/eng/notify-routes/DB4AI_WEBHOOK_ENG", headers=h).status_code == 404


# ------------------------------------------------------------------ auto_notice notices

def merge_pair(w):
    return item(w), item(w)


def test_auto_notice_change_is_announced_with_revert_deadline(world, sent, monkeypatch):
    w = world
    monkeypatch.setenv("DB4AI_NOTIFY_WEBHOOKS", "https://hooks.example/slack")
    monkeypatch.setenv("DB4AI_PUBLIC_URL", "https://db4ai.example")
    a, b = merge_pair(w)
    r = propose(w, "merge_items", {"keep": str(a), "drop": str(b)}, confidence=0.99)
    assert (r["tier"], r["status"]) == ("auto_notice", "applied_auto")
    assert len(notify_jobs(w)) == 1
    p = propose(w, "archive_item", {"item_id": str(item(w))})["decision_id"]  # same burst: one message
    drain(w)
    assert len(sent) == 1
    text = sent[0][1]["text"]
    assert "1 harness decision waiting for review (1 × archive_item)" in text
    assert "1 change applied automatically, revertible (1 × merge_items)" in text
    assert "applied for db4ai-maintainer" in text and "revert by " in text
    assert str(r["decision_id"]) in text and str(p) in text
    assert "https://db4ai.example/harness?status=applied_auto" in text
    assert "Nightingale" not in json.dumps(sent[0][1])
    rows = w.conn.execute("select decision_id from notifications").fetchall()
    assert {x["decision_id"] for x in rows} == {r["decision_id"], p}


def test_plain_auto_is_not_announced(world, sent, monkeypatch):
    w = world
    monkeypatch.setenv("DB4AI_NOTIFY_WEBHOOKS", "https://hooks.example/slack")
    propose(w, "add_tags", {"item_id": str(item(w)), "value": ["x"]}, confidence=1.0)
    assert notify_jobs(w) == []


def test_reverted_or_closed_window_is_not_announced(world, sent, monkeypatch):
    w = world
    monkeypatch.setenv("DB4AI_NOTIFY_WEBHOOKS", "https://hooks.example/slack")
    a, b = merge_pair(w)
    r1 = propose(w, "merge_items", {"keep": str(a), "drop": str(b)}, confidence=0.99)
    harness.revert(w.conn, w.bob, r1["decision_id"], note="different companies")
    c, d = merge_pair(w)
    r2 = propose(w, "merge_items", {"keep": str(c), "drop": str(d)}, confidence=0.99)
    w.conn.execute("update harness_decisions set revert_until = now() - interval '1 minute' where id=%s",
                   (r2["decision_id"],))
    drain(w)
    assert sent == []


def test_auto_notice_follows_team_routes(world, sent, monkeypatch):
    w = world
    monkeypatch.setenv("DB4AI_WEBHOOK_ENG", "https://hooks.example/eng")
    notify.add_route(w.conn, w.root, w.eng, "DB4AI_WEBHOOK_ENG")
    a, b = merge_pair(w)
    r = propose(w, "merge_items", {"keep": str(a), "drop": str(b)}, confidence=0.99)
    drain(w)
    assert [u for u, _ in sent] == ["https://hooks.example/eng"]
    assert str(r["decision_id"]) in sent[0][1]["text"]


def test_message_caps_across_both_sections():
    from datetime import datetime, timezone
    now = datetime(2026, 12, 1, tzinfo=timezone.utc)
    base = {"confidence": 0.99, "expires_at": None, "team": "eng", "proposed_by": "db4ai-maintainer"}
    pend = [{**base, "id": f"p{i}", "action_type": "archive_item", "status": "pending"} for i in range(15)]
    appl = [{**base, "id": f"a{i}", "action_type": "mark_stale", "status": "applied_auto", "revert_until": now}
            for i in range(10)]
    title, lines = notify.message(pend + appl, None)
    assert "15 harness decisions" in title and "10 changes applied automatically" in title
    assert sum(1 for ln in lines if ln.startswith("•")) == notify.MAX_LISTED
    assert "…and 5 more" in lines and "revert by 2026-12-01" in "\n".join(lines)
    title, lines = notify.message(appl[:1], None)
    assert title == "DB4AI: 1 change applied automatically, revertible (1 × mark_stale)"
    assert lines[-1].startswith("Revert:") and not any(ln.startswith("Review:") for ln in lines)
