"""Notion connector on the connector framework (SPEC §6): pages as `document` items.

The owner creates a Notion internal integration, shares the pages it should read with it, and supplies its token
through the environment (`DB4AI_NOTION_TOKEN`); DB4AI never stores it. The connector sees only pages shared with the
integration, which is how the owner chooses what syncs.

Each sync lists every page the integration can read with the search API (metadata only, 100 per request), then reads
the blocks of pages edited at or after the cursor and renders them as Markdown-ish text. Database rows are pages too
and are synced as documents (their body); their properties become dataset rows in a later release.

Cursor (opaque to the framework, never holds content): `v1:edited:<last_edited_time>` of the newest page synced.
Notion rounds `last_edited_time` to the minute, so pages at exactly the cursor time are re-sent next run (identical
content adds no version), like the transcripts connector. A cursor without the `v1:` tag starts a full listing.

Deletions: a page that is archived, in the trash, or no longer shared with the integration drops out of search, so
`deleted(known)` reports known keys missing from the listing. An empty listing reports nothing (fail closed: a token
that lost all its pages must not look like "everything deleted").

Notion's API doesn't expose who a page is shared with, so records carry no readers: synced pages are team-wide.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Callable, Iterator

import httpx

from .connectors import Batch, Record

log = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
TAG = "v1:"
MAX_BLOCKS = 10_000  # a page with more blocks is skipped and logged rather than half-synced
MAX_DEPTH = 8        # nesting depth read below a page
MAX_PAGES = 100_000  # a listing larger than this is refused
# Blocks whose children are their own pages / databases, or that hold no text: not read into the parent's body.
SKIP_CHILDREN = {"child_page", "child_database", "link_to_page", "unsupported"}


class NotionError(Exception):
    """Notion refused a request that a retry won't fix (bad token, bad request)."""


def _text(rich: list | None) -> str:
    return "".join(t.get("plain_text", "") for t in rich or [])


def _render(block: dict) -> str | None:
    """One block as a line of Markdown-ish text (None = nothing to show)."""
    t = block.get("type", "")
    v = block.get(t) or {}
    text = _text(v.get("rich_text"))
    if t in ("heading_1", "heading_2", "heading_3"):
        return "#" * int(t[-1]) + " " + text
    if t == "bulleted_list_item":
        return "- " + text
    if t == "numbered_list_item":
        return "1. " + text
    if t == "to_do":
        return ("- [x] " if v.get("checked") else "- [ ] ") + text
    if t == "quote":
        return "> " + text
    if t == "callout":
        icon = (v.get("icon") or {}).get("emoji")
        return f"> {icon} {text}" if icon else "> " + text
    if t == "code":
        return f"```{v.get('language') or ''}\n{text}\n```"
    if t == "equation":
        return f"$$ {v.get('expression', '')} $$"
    if t == "divider":
        return "---"
    if t == "table_row":
        return "| " + " | ".join(_text(c) for c in v.get("cells") or []) + " |"
    if t in ("child_page", "child_database"):
        return f"[{v.get('title', '')}]" if v.get("title") else None
    if t in ("bookmark", "embed", "link_preview"):
        cap = _text(v.get("caption"))
        return f"{cap} <{v.get('url')}>" if cap else f"<{v.get('url')}>" if v.get("url") else None
    if t in ("image", "file", "pdf", "video", "audio"):
        cap = _text(v.get("caption"))
        return f"[{t}: {cap}]" if cap else None
    return text or None  # paragraph, toggle, template, column, synced_block...


def page_title(page: dict) -> str:
    for p in (page.get("properties") or {}).values():
        if p.get("type") == "title":
            return _text(p.get("title")).strip() or "Untitled"
    return "Untitled"


def _when(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


class TooLarge(Exception):
    pass


class Notion:
    def __init__(self, token: str | None = None, http: httpx.Client | None = None, batch_size: int = 50,
                 retries: int = 4, sleep: Callable[[float], None] = time.sleep):
        self.token = token or os.environ.get("DB4AI_NOTION_TOKEN") or None
        if not self.token:
            raise NotionError("set DB4AI_NOTION_TOKEN to a Notion integration token")
        self.http = http or httpx.Client(timeout=60)
        self.batch_size, self.retries, self.sleep = batch_size, retries, sleep
        self._pages: list[dict] | None = None  # the listing, read once per instance (fetch and deleted share it)

    # ------------------------------------------------------------ HTTP

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        for attempt in range(self.retries + 1):
            r = self.http.request(method, f"{API}{path}", headers={
                "Authorization": f"Bearer {self.token}", "Notion-Version": NOTION_VERSION}, **kw)
            if (r.status_code == 429 or r.status_code >= 500) and attempt < self.retries:
                try:
                    wait = float(r.headers.get("retry-after", ""))
                except ValueError:
                    wait = 2 ** attempt
                self.sleep(min(max(wait, 0.0), 60))
                continue
            return r
        return r

    def _json(self, method: str, path: str, **kw) -> dict:
        r = self._request(method, path, **kw)
        if r.status_code != 200:
            raise NotionError(f"Notion {path} failed ({r.status_code}): {_reason(r)}")
        return r.json()

    # ------------------------------------------------------------ listing and content

    def pages(self) -> list[dict]:
        """Every page shared with the integration (not archived or trashed), oldest edit first."""
        if self._pages is None:
            found: dict[str, dict] = {}
            body: dict = {"filter": {"property": "object", "value": "page"}, "page_size": 100,
                          "sort": {"direction": "ascending", "timestamp": "last_edited_time"}}
            while True:
                out = self._json("POST", "/search", json=body)
                for p in out.get("results") or []:
                    if p.get("object") == "page" and not p.get("archived") and not p.get("in_trash"):
                        found[p["id"]] = p
                if len(found) > MAX_PAGES:
                    raise NotionError(f"more than {MAX_PAGES} pages are shared with the integration")
                if not out.get("has_more") or not out.get("next_cursor"):
                    break
                body = {**body, "start_cursor": out["next_cursor"]}
            self._pages = sorted(found.values(), key=lambda p: (_when(p["last_edited_time"]), p["id"]))
        return self._pages

    def _children(self, block_id: str) -> Iterator[dict]:
        params: dict = {"page_size": 100}
        while True:
            out = self._json("GET", f"/blocks/{block_id}/children", params=params)
            yield from out.get("results") or []
            if not out.get("has_more") or not out.get("next_cursor"):
                return
            params = {**params, "start_cursor": out["next_cursor"]}

    def _lines(self, block_id: str, depth: int, count: list[int], indent: str = "") -> list[str]:
        lines = []
        for b in self._children(block_id):
            count[0] += 1
            if count[0] > MAX_BLOCKS:
                raise TooLarge
            line = _render(b)
            if line is not None:
                lines.append("\n".join(indent + x for x in line.split("\n")))
            if b.get("has_children") and b.get("type") not in SKIP_CHILDREN and depth < MAX_DEPTH:
                nested = "  " if b.get("type") in ("bulleted_list_item", "numbered_list_item", "to_do") else ""
                lines.extend(self._lines(b["id"], depth + 1, count, indent + nested))
        return lines

    def content(self, page: dict) -> str | None:
        """The page's text, or None when it can't be read (unshared meanwhile, too large)."""
        try:
            lines = self._lines(page["id"], 1, [0])
        except TooLarge:
            log.warning("notion page %s has more than %d blocks; skipped", page["id"], MAX_BLOCKS)
            return None
        except NotionError as e:
            if "(404)" in str(e) or "(403)" in str(e):
                log.warning("notion page %s can't be read; skipped: %s", page["id"], e)
                return None
            raise
        return "\n".join(lines).strip()

    def _record(self, page: dict, body: str) -> Record:
        parent = page.get("parent") or {}
        ptype = parent.get("type", "")
        meta = {"url": page.get("url"), "last_edited_time": page["last_edited_time"],
                "created_time": page.get("created_time"), "parent_type": ptype}
        if ptype in ("page_id", "database_id", "block_id"):
            meta["parent_id"] = parent.get(ptype)
        return Record(key=page["id"], type="document", title=page_title(page), body=body,
                      metadata={k: v for k, v in meta.items() if v is not None})

    # ------------------------------------------------------------ connector protocol

    def fetch(self, cursor: str | None) -> Iterator[Batch]:
        since = None
        if cursor and cursor.startswith(TAG + "edited:"):
            since = _when(cursor.removeprefix(TAG + "edited:"))
        todo = [p for p in self.pages() if since is None or _when(p["last_edited_time"]) >= since]
        for i in range(0, len(todo), self.batch_size):
            chunk = todo[i:i + self.batch_size]
            records = []
            for p in chunk:
                body = self.content(p)
                if body is not None:
                    records.append(self._record(p, body))
            yield Batch(records, TAG + "edited:" + chunk[-1]["last_edited_time"])

    def deleted(self, known: set[str]) -> list[str]:
        """Synced pages no longer in the listing (archived, trashed, unshared). An empty listing reports nothing."""
        present = {p["id"] for p in self.pages()}
        if not present:
            if known:
                log.warning("no Notion pages are shared with the integration; not proposing %d deletions", len(known))
            return []
        return sorted(known - present)


def _reason(r: httpx.Response) -> str:
    try:
        j = r.json()
        return f"{j.get('code', '')}: {j.get('message', '')}".strip(": ")
    except Exception:
        return r.text[:200]
