"""Miro connector on the connector framework (SPEC §6): boards as `whiteboard` items.

The owner creates a Miro app in their own Miro team, installs it with the `boards:read` scope, and supplies its access
token through the environment (`DB4AI_MIRO_TOKEN`); DB4AI never stores it. The connector sees the boards that token's
user can open, optionally limited to one Miro team (`team` / `DB4AI_MIRO_TEAM`).

Record = one board (key = board id, title = board name). Its body is the board's text, read with the REST API v2
items endpoint: the description first, then each frame as a `## <frame title>` section (frames top to bottom, left to
right) holding the items placed in it, and the items outside any frame before the first frame. Items are ordered top to
bottom, then left to right; sticky notes and cards are `- ` bullets, cards with their description after a colon, text
and shapes as plain lines, images/documents/embeds by title or URL. Connector lines (arrows) aren't rendered.

Access fails closed: Miro's API lists board members by name only (no emails), so readers can't be mapped to actors.
A board is synced only when its sharing policy opens it beyond its members (team, organization or link access other
than `private`), and is then team-wide; a private board is skipped (and, if synced before, reported deleted).

Cursor (opaque to the framework, never holds content): `v1:scope:<hash of the team id>:modified:<modifiedAt>` of the
newest board synced. Boards modified at or after the cursor are re-read (those at exactly the cursor time are re-sent;
identical content adds no version). A cursor from another scope, or any other cursor, re-lists every board.

Deletions: a synced board no longer listed (deleted, access lost) or now private is reported to `deleted` (→
`archive_item`, ask_first). An empty listing reports nothing (fail closed). A board whose items can't be read this run
(403/404) or with more than `MAX_ITEMS` items is skipped and logged, and isn't reported deleted.
"""

from __future__ import annotations

import hashlib
import html
import logging
import os
import re
import time
from datetime import datetime
from typing import Callable, Iterator

import httpx

from .connectors import Batch, Record

log = logging.getLogger(__name__)

API = "https://api.miro.com/v2"
TAG = "v1:"
MAX_BOARDS = 10_000
MAX_ITEMS = 10_000   # a board with more items is skipped and logged rather than half-synced
PAGE = 50            # Miro's maximum page size for boards and items
BULLETS = {"sticky_note", "card", "app_card"}


class MiroError(Exception):
    """Miro refused a request that a retry won't fix (bad token, bad request)."""


class TooLarge(Exception):
    pass


_BREAK = re.compile(r"(?i)<\s*(br\s*/?|/p|/li|/div|/h[1-6])\s*>")
_TAG = re.compile(r"<[^>]+>")


def html_text(s: str | None) -> str:
    """Miro's rich text (a little HTML) as plain text: paragraphs and line breaks become spaces between lines."""
    if not s:
        return ""
    lines = (html.unescape(_TAG.sub("", part)) for part in _BREAK.split(s)[::2])
    return " ".join(" ".join(x.split()) for x in lines if x.strip())


def _when(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _pos(item: dict) -> tuple[float, float, str]:
    p = item.get("position") or {}
    return (float(p.get("y") or 0), float(p.get("x") or 0), item.get("id", ""))


def item_line(item: dict) -> str | None:
    """One board item as a line of text (None = nothing to show)."""
    t = item.get("type", "")
    d = item.get("data") or {}
    if t in ("card", "app_card"):
        title, desc = html_text(d.get("title")), html_text(d.get("description"))
        text = f"{title}: {desc}" if title and desc else title or desc
    elif t in ("image", "document"):
        title = html_text(d.get("title"))
        return f"[{t}: {title}]" if title else None
    elif t == "embed":
        url = d.get("url")
        return f"<{url}>" if url else None
    else:  # sticky_note, text, shape, ...
        text = html_text(d.get("content"))
    if not text:
        return None
    return "- " + text if t in BULLETS else text


def is_private(board: dict) -> bool:
    """True unless the board's sharing policy opens it beyond its members. A missing policy counts as private."""
    sp = ((board.get("policy") or {}).get("sharingPolicy")) or {}
    if not sp:
        return True
    return all((sp.get(k) or "private") == "private" for k in ("access", "teamAccess", "organizationAccess"))


class Miro:
    def __init__(self, token: str | None = None, http: httpx.Client | None = None, team: str | None = None,
                 batch_size: int = 20, retries: int = 4, sleep: Callable[[float], None] = time.sleep):
        self.token = token or os.environ.get("DB4AI_MIRO_TOKEN") or None
        if not self.token:
            raise MiroError("set DB4AI_MIRO_TOKEN to a Miro access token with the boards:read scope")
        self.team = team or os.environ.get("DB4AI_MIRO_TEAM") or None
        self.http = http or httpx.Client(timeout=60)
        self.batch_size, self.retries, self.sleep = batch_size, retries, sleep
        self._boards: list[dict] | None = None   # the listing, read once per instance (fetch and deleted share it)
        self.skipped: set[str] = set()           # listed boards that couldn't be read this run

    # ------------------------------------------------------------ HTTP

    def _request(self, path: str, params: dict | None = None) -> httpx.Response:
        for attempt in range(self.retries + 1):
            r = self.http.get(f"{API}{path}", params=params, headers={"Authorization": f"Bearer {self.token}"})
            if (r.status_code == 429 or r.status_code >= 500) and attempt < self.retries:
                try:
                    wait = float(r.headers.get("retry-after", ""))
                except ValueError:
                    wait = 2 ** attempt
                self.sleep(min(max(wait, 0.0), 60))
                continue
            return r
        return r

    def _json(self, path: str, params: dict | None = None) -> dict:
        r = self._request(path, params)
        if r.status_code != 200:
            raise MiroError(f"Miro {path} failed ({r.status_code}): {_reason(r)}")
        return r.json()

    # ------------------------------------------------------------ listing and content

    def scope(self) -> str:
        return hashlib.sha256((self.team or "").encode()).hexdigest()[:12]

    def boards(self) -> list[dict]:
        """Every board the token can open (in the team, if set), private or not, oldest modification first."""
        if self._boards is None:
            found: dict[str, dict] = {}
            offset = 0
            while True:
                params: dict = {"limit": PAGE, "offset": offset}
                if self.team:
                    params["team_id"] = self.team
                out = self._json("/boards", params)
                data = out.get("data") or []
                for b in data:
                    found[b["id"]] = b
                if len(found) > MAX_BOARDS:
                    raise MiroError(f"more than {MAX_BOARDS} Miro boards are visible to the token")
                offset += len(data)
                if not data or offset >= int(out.get("total") or 0):
                    break
            self._boards = sorted(found.values(), key=lambda b: (_when(b["modifiedAt"]), b["id"]))
        return self._boards

    def _items(self, board_id: str) -> list[dict]:
        items: list[dict] = []
        params: dict = {"limit": PAGE}
        while True:
            r = self._request(f"/boards/{board_id}/items", params)
            if r.status_code != 200:
                raise MiroError(f"Miro /boards/{board_id}/items failed ({r.status_code}): {_reason(r)}")
            out = r.json()
            items += out.get("data") or []
            if len(items) > MAX_ITEMS:
                raise TooLarge
            if not out.get("cursor"):
                return items
            params = {"limit": PAGE, "cursor": out["cursor"]}

    def content(self, board: dict) -> str | None:
        """The board's text, or None when it can't be read (access lost meanwhile, too large)."""
        try:
            items = self._items(board["id"])
        except TooLarge:
            log.warning("miro board %s has more than %d items; skipped", board["id"], MAX_ITEMS)
            return None
        except MiroError as e:
            if "(404)" in str(e) or "(403)" in str(e):
                log.warning("miro board %s can't be read; skipped: %s", board["id"], e)
                return None
            raise
        frames = sorted((i for i in items if i.get("type") == "frame"), key=_pos)
        frame_ids = {f["id"] for f in frames}
        inside: dict[str, list[dict]] = {f["id"]: [] for f in frames}
        loose: list[dict] = []
        for i in items:
            if i.get("type") in ("frame", "connector"):
                continue
            parent = (i.get("parent") or {}).get("id")
            (inside[parent] if parent in frame_ids else loose).append(i)
        out: list[str] = []
        desc = html_text(board.get("description"))
        if desc:
            out.append(desc)
        out += [x for x in (item_line(i) for i in sorted(loose, key=_pos)) if x]
        for f in frames:
            if out:
                out.append("")
            out.append("## " + (html_text((f.get("data") or {}).get("title")) or "Frame"))
            out += [x for x in (item_line(i) for i in sorted(inside[f["id"]], key=_pos)) if x]
        return "\n".join(out).strip()

    def _record(self, board: dict, body: str) -> Record:
        meta = {"view_link": board.get("viewLink"), "modified_at": board.get("modifiedAt"),
                "created_at": board.get("createdAt"), "team_id": (board.get("team") or {}).get("id")}
        return Record(key=board["id"], type="whiteboard", title=board.get("name") or f"miro {board['id']}", body=body,
                      metadata={k: v for k, v in meta.items() if v is not None})

    # ------------------------------------------------------------ connector protocol

    def fetch(self, cursor: str | None) -> Iterator[Batch]:
        prefix = f"{TAG}scope:{self.scope()}:modified:"
        since_s = cursor.removeprefix(prefix) if cursor and cursor.startswith(prefix) else ""
        since = _when(since_s) if since_s else None
        listed = self.boards()
        private = [b for b in listed if is_private(b)]
        if private:
            log.info("%d private Miro boards skipped (not shared beyond their members)", len(private))
        todo = [b for b in listed if not is_private(b) and (since is None or _when(b["modifiedAt"]) >= since)]
        here = prefix + since_s
        for i in range(0, len(todo), self.batch_size):
            chunk = todo[i:i + self.batch_size]
            records = []
            for b in chunk:
                body = self.content(b)
                if body is None:
                    self.skipped.add(b["id"])
                else:
                    records.append(self._record(b, body))
            here = prefix + chunk[-1]["modifiedAt"]
            yield Batch(records, here)
        if not todo and here != cursor:
            yield Batch([], here)

    def deleted(self, known: set[str]) -> list[str]:
        """Synced boards no longer listed or now private. An empty listing reports nothing."""
        listed = self.boards()
        if not listed:
            if known:
                log.warning("no Miro boards are visible to the token; not proposing %d deletions", len(known))
            return []
        present = {b["id"] for b in listed if not is_private(b)} | self.skipped
        return sorted(known - present)


def _reason(r: httpx.Response) -> str:
    try:
        j = r.json()
        return f"{j.get('code', '')}: {j.get('message', '')}".strip(": ")
    except Exception:
        return r.text[:200]
