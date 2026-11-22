import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from db4ai import harness, notify, store, worker
from db4ai.auth import maintainer_agent
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
