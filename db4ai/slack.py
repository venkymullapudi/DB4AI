"""Slack connector on the connector framework (SPEC §6): channel messages as `conversation` items, one per day.

The owner creates a Slack app in their own workspace (an internal app), adds a bot token with the scopes
`channels:read`, `groups:read`, `channels:history`, `groups:history`, `users:read` and `users:read.email`, and invites
the bot to the channels that should sync; the token comes from the environment (`DB4AI_SLACK_TOKEN`) and DB4AI never
stores it. Direct messages and group DMs are never read.

Record = one channel's messages on one UTC day (key `<channel id>/<YYYY-MM-DD>`, title `#<name> <day>`), rendered as
`[HH:MM] Name: text` lines with each thread's replies indented under its parent (a thread lives in its parent's day).
One item per message would flood search with fragments; one per channel would be a document that never stops growing.

Each sync re-reads a window of every channel and re-sends each day in it whole, so edits, deleted messages and new
thread replies in that window are picked up (identical days add no version): a channel already synced is re-read from
the UTC midnight `LOOKBACK_DAYS` before the previous sync; a channel the bot newly joined (or every channel on the
first sync) is backfilled `days` days (default 30). Changes to days older than the window are not seen.

Cursor (opaque to the framework, never holds content): `v1:ts:<unix time the sync started>:ch:<channel ids>`, the ids
of the channels synced so far (so a newly joined channel is backfilled). Batches go out at the incoming cursor (a crash
re-reads, idempotently) and a final batch moves it.

Access: a private channel's days are restricted to its members (by email, `reader_emails`; members without a matching
actor are dropped, so access only narrows), a public channel's days are sent as `public` (a channel made public lifts
the restriction through an ask_first proposal).

Deletions: a day in a channel's re-read window that was synced before and now has no messages, and every synced day
of a channel the bot is no longer in (left, removed, channel deleted), are reported to `deleted` (→ `archive_item`,
ask_first). A channel whose history can't be read this run, or a listing with no channels at all, reports nothing.
"""

from __future__ import annotations

import html
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Callable, Iterator

import httpx

from .connectors import Batch, Record

log = logging.getLogger(__name__)

API = "https://slack.com/api"
TAG = "v1:"
DAY = 86_400
LOOKBACK_DAYS = 7
MAX_CHANNELS = 5_000
# Housekeeping messages that carry no conversation.
SKIP_SUBTYPES = {"channel_join", "channel_leave", "group_join", "group_leave", "channel_topic", "channel_purpose",
                 "channel_name", "channel_archive", "channel_unarchive", "group_topic", "group_purpose", "group_name",
                 "group_archive", "group_unarchive", "pinned_item", "unpinned_item", "tombstone"}
# Errors that mean "this channel can't be read now": skip it (and report none of its days deleted).
CHANNEL_ERRORS = {"not_in_channel", "channel_not_found", "missing_scope", "access_denied", "is_archived"}

_REF = re.compile(r"<([^<>|]+)(?:\|([^<>]*))?>")


class SlackError(Exception):
    """Slack refused a request that a retry won't fix (bad token, missing scope)."""


class ChannelUnreadable(Exception):
    pass


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M")


def _midnight(ts: float) -> float:
    return float(int(ts) // DAY * DAY)


class Slack:
    def __init__(self, token: str | None = None, http: httpx.Client | None = None, days: int = 30,
                 batch_size: int = 100, retries: int = 4, sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], float] = time.time):
        self.token = token or os.environ.get("DB4AI_SLACK_TOKEN") or None
        if not self.token:
            raise SlackError("set DB4AI_SLACK_TOKEN to a Slack bot token")
        if days < 1:
            raise ValueError("days must be at least 1")
        self.http = http or httpx.Client(timeout=60)
        self.days, self.batch_size, self.retries, self.sleep, self.now = days, batch_size, retries, sleep, now
        self._channels: list[dict] | None = None
        self._users: dict[str, dict] | None = None
        # Filled by fetch, used by deleted: per readable channel, the window start and the days that had messages.
        self._windows: dict[str, float] = {}
        self._days: dict[str, set[str]] = {}
        self._unreadable: set[str] = set()

    # ------------------------------------------------------------ HTTP

    def _call(self, method: str, **params) -> dict:
        for attempt in range(self.retries + 1):
            r = self.http.get(f"{API}/{method}", params=params, headers={"Authorization": f"Bearer {self.token}"})
            retry = r.status_code == 429 or r.status_code >= 500
            if not retry and r.status_code == 200:
                out = r.json()
                if out.get("ok"):
                    return out
                retry = out.get("error") == "ratelimited"
                if not retry:
                    err = out.get("error", "unknown_error")
                    if err in CHANNEL_ERRORS and "channel" in params:
                        raise ChannelUnreadable(err)
                    raise SlackError(f"Slack {method} failed: {err}")
            if retry and attempt < self.retries:
                try:
                    wait = float(r.headers.get("retry-after", ""))
                except ValueError:
                    wait = 2 ** attempt
                self.sleep(min(max(wait, 0.0), 60))
                continue
            raise SlackError(f"Slack {method} failed ({r.status_code})")
        raise SlackError(f"Slack {method} failed")  # pragma: no cover

    def _paged(self, method: str, key: str, **params) -> Iterator[dict]:
        cursor = ""
        while True:
            out = self._call(method, limit=200, **({"cursor": cursor} if cursor else {}), **params)
            yield from out.get(key) or []
            cursor = (out.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                return

    # ------------------------------------------------------------ listing

    def channels(self) -> list[dict]:
        """Public and private channels the bot is a member of (archived ones included: their history stays)."""
        if self._channels is None:
            found = {}
            for c in self._paged("users.conversations", "channels", types="public_channel,private_channel",
                                 exclude_archived="false"):
                found[c["id"]] = c
                if len(found) > MAX_CHANNELS:
                    raise SlackError(f"the bot is in more than {MAX_CHANNELS} channels")
            self._channels = sorted(found.values(), key=lambda c: c["id"])
        return self._channels

    def users(self) -> dict[str, dict]:
        if self._users is None:
            self._users = {u["id"]: u for u in self._paged("users.list", "members")}
        return self._users

    def _name(self, uid: str | None, msg: dict | None = None) -> str:
        u = self.users().get(uid or "")
        if u:
            p = u.get("profile") or {}
            return p.get("display_name") or p.get("real_name") or u.get("real_name") or u.get("name") or uid
        if msg:
            return (msg.get("bot_profile") or {}).get("name") or msg.get("username") or uid or "unknown"
        return uid or "unknown"

    def _email(self, uid: str) -> str | None:
        u = self.users().get(uid) or {}
        if u.get("deleted") or u.get("is_bot"):
            return None
        return (u.get("profile") or {}).get("email") or None

    # ------------------------------------------------------------ rendering

    def text(self, msg: dict) -> str:
        """Slack mrkdwn → plain text: mentions and channel refs by name, links as `label (url)`."""

        def ref(m: re.Match) -> str:
            target, label = m.group(1), m.group(2)
            if target.startswith("@"):
                return "@" + (label or self._name(target[1:]))
            if target.startswith("#"):
                return "#" + (label or target[1:])
            if target.startswith("!"):
                return "@" + (label or target[1:].split("^")[0])
            return f"{label} ({target})" if label and label != target else target

        out = html.unescape(_REF.sub(ref, msg.get("text") or ""))
        files = [f.get("name") or f.get("title") for f in msg.get("files") or []]
        files = [f for f in files if f]
        if files:
            out = (out + " " if out else "") + " ".join(f"[file: {f}]" for f in files)
        return out.strip()

    def _line(self, msg: dict, indent: str = "") -> str | None:
        if msg.get("subtype") in SKIP_SUBTYPES:
            return None
        text = self.text(msg)
        if not text:
            return None
        lines = text.split("\n")
        head = f"{indent}[{_hhmm(float(msg['ts']))}] {self._name(msg.get('user'), msg)}: {lines[0]}"
        return "\n".join([head, *(indent + "    " + x for x in lines[1:])])

    # ------------------------------------------------------------ history

    def _history(self, channel: str, oldest: float) -> list[dict]:
        msgs = list(self._paged("conversations.history", "messages", channel=channel, oldest=f"{oldest:.6f}",
                                inclusive="true"))
        # A reply also sent to the channel shows up in history; it is rendered inside its thread instead.
        return sorted((m for m in msgs if not m.get("thread_ts") or m["thread_ts"] == m["ts"]),
                      key=lambda m: float(m["ts"]))

    def _replies(self, channel: str, ts: str) -> list[dict]:
        return sorted((m for m in self._paged("conversations.replies", "messages", channel=channel, ts=ts)
                       if m["ts"] != ts), key=lambda m: float(m["ts"]))

    def _render_days(self, channel: dict, oldest: float) -> dict[str, tuple[list[str], set[str], int]]:
        days: dict[str, tuple[list[str], set[str], int]] = {}
        for m in self._history(channel["id"], oldest):
            replies = self._replies(channel["id"], m["ts"]) if m.get("reply_count") else []
            shown = [x for x in [m, *replies] if self._line(x) is not None]
            if not shown:
                continue
            lines = [] if shown[0] is m else [f"[{_hhmm(float(m['ts']))}] (message deleted)"]
            lines += [self._line(x, "" if x is m else "    ↳ ") for x in shown]
            day = _day(float(m["ts"]))
            body, people, count = days.get(day, ([], set(), 0))
            body.extend(lines)
            people.update(self._name(x.get("user"), x) for x in shown)
            days[day] = (body, people, count + len(shown))
        return days

    def _access(self, channel: dict) -> dict:
        if not channel.get("is_private"):
            return {"public": True}
        emails = []
        for uid in self._paged("conversations.members", "members", channel=channel["id"]):
            e = self._email(uid)
            if e:
                emails.append(e.lower())
        return {"reader_emails": sorted(set(emails))}

    def records(self, channel: dict, oldest: float) -> list[Record]:
        days = self._render_days(channel, oldest)
        self._days[channel["id"]] = set(days)
        if not days:
            return []
        access = self._access(channel)
        name = channel.get("name") or channel["id"]
        return [Record(key=f"{channel['id']}/{day}", type="conversation", title=f"#{name} {day}",
                       body="\n".join(body),
                       metadata={"channel_id": channel["id"], "channel_name": name, "date": day,
                                 "private": bool(channel.get("is_private")), "messages": count,
                                 "participants": sorted(people)},
                       **access)
                for day, (body, people, count) in sorted(days.items())]

    # ------------------------------------------------------------ connector protocol

    def fetch(self, cursor: str | None) -> Iterator[Batch]:
        started = self.now()
        since, seen = None, set()
        if cursor and cursor.startswith(TAG + "ts:") and ":ch:" in cursor:
            ts, ids = cursor.removeprefix(TAG + "ts:").split(":ch:", 1)
            try:
                since, seen = float(ts), {i for i in ids.split(",") if i}
            except ValueError:
                since, seen = None, set()
        synced = set()
        for ch in self.channels():
            if since is not None and ch["id"] in seen:
                oldest = _midnight(min(since, started) - LOOKBACK_DAYS * DAY)
            else:
                oldest = _midnight(started - self.days * DAY)
            try:
                recs = self.records(ch, oldest)
            except ChannelUnreadable as e:
                log.warning("slack channel %s can't be read; skipped: %s", ch["id"], e)
                self._unreadable.add(ch["id"])
                if ch["id"] in seen:
                    synced.add(ch["id"])  # keep it known, so it isn't backfilled as new when it comes back
                continue
            self._windows[ch["id"]] = oldest
            synced.add(ch["id"])
            for i in range(0, len(recs), self.batch_size):
                yield Batch(recs[i:i + self.batch_size], cursor)
        here = f"{TAG}ts:{int(started)}:ch:{','.join(sorted(synced))}"
        if here != cursor:
            yield Batch([], here)

    def deleted(self, known: set[str]) -> list[str]:
        """Synced days that are gone: emptied days inside a channel's re-read window, and every day of a channel
        the bot is no longer in. No channels listed at all reports nothing (fail closed)."""
        listed = {c["id"] for c in self.channels()}
        if not listed:
            if known:
                log.warning("the Slack bot is in no channels; not proposing %d deletions", len(known))
            return []
        gone = []
        for key in known:
            ch, _, day = key.partition("/")
            if ch not in listed:
                gone.append(key)
            elif ch in self._windows and day >= _day(self._windows[ch]) and day not in self._days.get(ch, set()):
                gone.append(key)
        return sorted(gone)
