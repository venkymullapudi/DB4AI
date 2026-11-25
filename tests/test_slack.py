import json
from datetime import datetime, timezone

import httpx
import pytest

from db4ai import auth, connectors, store
from db4ai.cli import main
from db4ai.slack import Slack, SlackError


def T(s):
    """'2026-11-24 09:30' → Slack ts string."""
    return f"{datetime.strptime(s, '%Y-%m-%d %H:%M').replace(tzinfo=timezone.utc).timestamp():.6f}"


NOW = float(T("2026-11-25 12:00"))


class FakeSlack:
    """users.conversations, users.list, conversations.history/replies/members, paged by `limit`/`cursor`."""

    def __init__(self, page_size=200):
        self.channels: dict[str, dict] = {}
        self.msgs: dict[str, list[dict]] = {}
        self.replies: dict[tuple[str, str], list[dict]] = {}
        self.members: dict[str, list[str]] = {}
        self.users = [
            {"id": "UA", "name": "alice", "profile": {"display_name": "Alice", "email": "alice@acme.com"}},
            {"id": "UB", "name": "bob", "profile": {"real_name": "Bob B", "email": "Bob@acme.com"}},
            {"id": "UC", "name": "carol", "profile": {"display_name": "Carol", "email": "carol@acme.com"}},
            {"id": "UX", "name": "bot", "is_bot": True, "profile": {"display_name": "Bot", "email": "bot@acme.com"}},
        ]
        self.page_size = page_size
        self.fail_next: list = []
        self.unreadable: set[str] = set()
        self.requests: list[httpx.Request] = []

    def channel(self, cid, name, private=False, members=()):
        self.channels[cid] = {"id": cid, "name": name, "is_private": private, "is_archived": False}
        self.msgs.setdefault(cid, [])
        self.members[cid] = list(members)

    def post(self, cid, when, user, text, thread=None, **extra):
        m = {"type": "message", "ts": T(when), "user": user, "text": text, **extra}
        if thread:
            m["thread_ts"] = thread
            self.replies.setdefault((cid, thread), []).append(m)
            parent = next(p for p in self.msgs[cid] if p["ts"] == thread)
            parent["thread_ts"], parent["reply_count"] = thread, parent.get("reply_count", 0) + 1
        else:
            self.msgs[cid].append(m)
        return m["ts"]

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if self.fail_next:
            f = self.fail_next.pop(0)
            if f == "ratelimited":
                return httpx.Response(200, json={"ok": False, "error": "ratelimited"})
            return httpx.Response(f, headers={"retry-after": "2"})
        if req.headers.get("authorization") != "Bearer xoxb-secret":
            return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})
        method, q = req.url.path.removeprefix("/api/"), req.url.params
        ch = q.get("channel")
        if ch is not None and (ch in self.unreadable or ch not in self.channels):
            return httpx.Response(200, json={"ok": False, "error": "not_in_channel"})
        if method == "users.conversations":
            assert q["types"] == "public_channel,private_channel"
            return self._paged(q, "channels", [self.channels[c] for c in sorted(self.channels)])
        if method == "users.list":
            return self._paged(q, "members", self.users)
        if method == "conversations.members":
            return self._paged(q, "members", self.members[ch])
        if method == "conversations.history":
            oldest = float(q["oldest"])
            return self._paged(q, "messages", sorted((m for m in self.msgs[ch] if float(m["ts"]) >= oldest),
                                                     key=lambda m: -float(m["ts"])))  # newest first, like Slack
        if method == "conversations.replies":
            parent = next(m for m in self.msgs[ch] if m["ts"] == q["ts"])
            return self._paged(q, "messages", [parent, *self.replies.get((ch, q["ts"]), [])])
        raise AssertionError(method)

    def _paged(self, q, key, objs):
        size = min(int(q.get("limit", 200)), self.page_size)
        start = int(q.get("cursor") or 0)
        more = start + size < len(objs)
        return httpx.Response(200, json={"ok": True, key: objs[start:start + size],
                                         "response_metadata": {"next_cursor": str(start + size) if more else ""}})


def slack(fake, token="xoxb-secret", now=NOW, **kw):
    return Slack(token, httpx.Client(transport=httpx.MockTransport(fake.handler)), sleep=lambda s: None,
                 now=lambda: now, **kw)


def _item(w, actor, key):
    row = w.conn.execute("select id from items where external_key=%s", (f"slack:{key}",)).fetchone()
    return store.get_item(w.conn, actor, row["id"]) if row else None


def test_channels_sync_as_days(world):
    w = world
    fake = FakeSlack(page_size=1)
    fake.channel("C1", "general")
    parent = fake.post("C1", "2026-11-24 09:30", "UA", "Ship <https://acme.com/p|pricing> on Friday, <@UB>?")
    fake.post("C1", "2026-11-24 09:40", "UB", "yes &amp; legal agrees", thread=parent)
    fake.post("C1", "2026-11-24 10:00", "UX", "deploy done", bot_profile={"name": "CI"})
    fake.post("C1", "2026-11-24 10:05", "UC", "", subtype="channel_join")
    fake.post("C1", "2026-11-25 08:00", "UC", "new day\nsecond line", files=[{"name": "plan.pdf"}])
    fake.post("C1", "2026-10-01 08:00", "UC", "too old for the backfill")

    r = connectors.run(w.conn, w.bob, "slack", w.eng, slack(fake))
    assert (r["saved"], r["changed"]) == (2, 2)
    assert r["cursor"] == f"v1:ts:{int(NOW)}:ch:C1"
    day = _item(w, w.alice, "C1/2026-11-24")
    assert day["type"] == "conversation" and day["title"] == "#general 2026-11-24"
    assert day["source"] == "connector:slack"
    assert day["body"] == ("[09:30] Alice: Ship pricing (https://acme.com/p) on Friday, @Bob B?\n"
                           "    ↳ [09:40] Bob B: yes & legal agrees\n"
                           "[10:00] Bot: deploy done")
    assert day["metadata"] == {"channel_id": "C1", "channel_name": "general", "date": "2026-11-24",
                               "private": False, "messages": 3, "participants": ["Alice", "Bob B", "Bot"]}
    assert _item(w, w.alice, "C1/2026-11-25")["body"] == "[08:00] Carol: new day\n    second line [file: plan.pdf]"
    assert _item(w, w.alice, "C1/2026-10-01") is None

    # Idle re-sync writes nothing; a new reply in the window is a new version of its parent's day.
    assert connectors.run(w.conn, w.bob, "slack", w.eng, slack(fake, now=NOW + 3600))["changed"] == 0
    fake.post("C1", "2026-11-25 11:00", "UC", "late reply", thread=parent)
    r = connectors.run(w.conn, w.bob, "slack", w.eng, slack(fake, now=NOW + 7200))
    assert r["changed"] == 1
    assert _item(w, w.alice, "C1/2026-11-24")["body"].endswith("    ↳ [11:00] Carol: late reply\n[10:00] Bot: deploy done")

    # A channel joined later is backfilled; known channels only re-read their window.
    fake.channel("C2", "random")
    fake.post("C2", "2026-11-01 12:00", "UA", "older than the lookback but inside the backfill")
    fake.requests.clear()
    r = connectors.run(w.conn, w.bob, "slack", w.eng, slack(fake, now=NOW + 7200))
    assert r["changed"] == 1 and r["cursor"].endswith(":ch:C1,C2")
    hist = {q.url.params["channel"]: float(q.url.params["oldest"]) for q in fake.requests
            if q.url.path.endswith("conversations.history")}
    assert hist == {"C1": float(T("2026-11-18 00:00")), "C2": float(T("2026-10-26 00:00"))}


def test_private_channels_restricted_to_members(world):
    w = world
    with w.conn.transaction():
        auth.set_email(w.conn, "alice", "alice@acme.com")
        auth.set_email(w.conn, "bob", "bob@acme.com")
        auth.set_email(w.conn, "carol", "carol@acme.com")  # not in eng: dropped
    fake = FakeSlack()
    fake.channel("P1", "leadership", private=True, members=["UA", "UC", "UX"])
    fake.post("P1", "2026-11-24 09:00", "UA", "secret reorg plan")
    fake.channel("C1", "general")
    fake.post("C1", "2026-11-24 09:00", "UA", "hello all")

    r = connectors.run(w.conn, w.alice, "slack", w.eng, slack(fake))
    assert r["saved"] == 2
    assert _item(w, w.alice, "P1/2026-11-24")["readers"] == ["alice"]
    with pytest.raises(LookupError):
        store.get_item(w.conn, w.bob, _item(w, w.alice, "P1/2026-11-24")["id"])
    assert _item(w, w.bob, "C1/2026-11-24")["body"] == "[09:00] Alice: hello all"

    # Bob joins the private channel: the next sync proposes widening the ACL to him (ask_first).
    fake.members["P1"].append("UB")
    r = connectors.run(w.conn, w.alice, "slack", w.eng, slack(fake, now=NOW + 60))
    assert r["access_proposed"] == 1


def test_deletions_and_fail_closed(world):
    w = world
    fake = FakeSlack()
    fake.channel("C1", "general")
    fake.channel("C2", "random")
    gone = fake.post("C1", "2026-11-23 09:00", "UA", "posted then deleted")
    fake.post("C1", "2026-11-24 09:00", "UA", "stays")
    fake.post("C2", "2026-11-24 09:00", "UB", "random chatter")
    connectors.run(w.conn, w.bob, "slack", w.eng, slack(fake))

    # A day emptied inside the window is a deletion; so is every day of a channel the bot left.
    fake.msgs["C1"] = [m for m in fake.msgs["C1"] if m["ts"] != gone]
    del fake.channels["C2"]
    r = connectors.run(w.conn, w.bob, "slack", w.eng, slack(fake, now=NOW + 60))
    assert r["deleted"] == 2
    keys = {d["payload"]["item_id"] for d in w.conn.execute(
        "select payload from harness_decisions where action_type='archive_item' and status='pending'").fetchall()}
    assert keys == {str(_item(w, w.alice, "C1/2026-11-23")["id"]), str(_item(w, w.alice, "C2/2026-11-24")["id"])}

    # A channel that can't be read this run, or no channels at all, proposes nothing.
    fake.post("C1", "2026-11-25 09:00", "UA", "today")
    fake.unreadable.add("C1")
    r = connectors.run(w.conn, w.bob, "slack", w.eng, slack(fake, now=NOW + 120))
    assert r["deleted"] == 0 and r["cursor"].endswith(":ch:C1")
    fake.channels.clear()
    assert connectors.run(w.conn, w.bob, "slack", w.eng, slack(fake, now=NOW + 180))["deleted"] == 0


def test_retries_and_errors(world):
    w = world
    fake = FakeSlack()
    fake.channel("C1", "general")
    fake.post("C1", "2026-11-24 09:00", "UA", "hi")
    fake.fail_next = [429, "ratelimited", 503]
    slept = []
    s = Slack("xoxb-secret", httpx.Client(transport=httpx.MockTransport(fake.handler)), sleep=slept.append,
              now=lambda: NOW)
    assert connectors.run(w.conn, w.bob, "slack", w.eng, s)["saved"] == 1
    assert slept == [2.0, 2.0, 2.0]
    with pytest.raises(SlackError, match="invalid_auth"):
        connectors.run(w.conn, w.bob, "slack", w.eng, slack(fake, token="wrong"))
    with pytest.raises(ValueError):
        slack(fake, days=0)


def test_cli_slack_needs_token(db_url, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DB4AI_DATABASE_URL", db_url)
    monkeypatch.setenv("DB4AI_BLOB_URL", f"file://{tmp_path}/blobs")
    monkeypatch.delenv("DB4AI_SLACK_TOKEN", raising=False)
    main(["user", "dana", "--team", "eng:owner"])
    json.loads(capsys.readouterr().out)
    with pytest.raises(SystemExit, match="DB4AI_SLACK_TOKEN"):
        main(["sync", "slack", "--team", "eng", "--as", "dana"])
