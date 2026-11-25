"""Notion connector on the connector framework (SPEC §6): pages as `document` items, databases as datasets.

The owner creates a Notion internal integration, shares the pages it should read with it, and supplies its token
through the environment (`DB4AI_NOTION_TOKEN`); DB4AI never stores it. The connector sees only pages shared with the
integration, which is how the owner chooses what syncs.

Each sync lists every page the integration can read with the search API (metadata only, 100 per request), then reads
the blocks of pages edited at or after the cursor and renders them as Markdown-ish text. Database rows are pages too
and are synced as documents (their body).

Each database shared with the integration is also a dataset (key = database id): its rows are read with the database
query API and sent as a full snapshot (`page_id`, the title property, then the other properties by name; every value
as text), which the framework diffs into `data.<team>_<name>`; property changes become `alter_dataset` proposals. A
database is re-read when it or one of its rows (from the page listing) was edited at or after the cursor, or when the
set of databases and rows changed since the last sync (a row deleted or a database shared), which the cursor tracks as
a hash. Database batches go first, at the incoming cursor, so a crash re-reads them; the cursor moves after them.

Cursor (opaque to the framework, never holds content): `v2:rows:<hash>:edited:<last_edited_time>` of the newest page
synced. Notion rounds `last_edited_time` to the minute, so pages at exactly the cursor time are re-sent next run
(identical content adds no version), like the transcripts connector. A `v1:edited:` cursor keeps its time and re-reads
every database once; any other cursor starts a full listing.

Deletions: a page that is archived, in the trash, or no longer shared with the integration drops out of search, so
`deleted(known)` reports known keys missing from the listing. An empty listing reports nothing (fail closed: a token
that lost all its pages must not look like "everything deleted"). Databases likewise: one no longer listed is reported
by `deleted_datasets` (→ `archive_dataset`), unless neither pages nor databases are listed at all.

Notion's API doesn't expose who a page is shared with, so records carry no readers: synced pages are team-wide.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import datetime
from typing import Callable, Iterator

import httpx

from . import datasets
from .connectors import Batch, DatasetRecord, Record

log = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
TAG = "v2:"
OLD_TAG = "v1:"
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


def _user(u: dict | None) -> str:
    u = u or {}
    return u.get("name") or (u.get("person") or {}).get("email") or u.get("id") or ""


def _date(d: dict | None) -> str:
    if not d or not d.get("start"):
        return ""
    return f"{d['start']} → {d['end']}" if d.get("end") else d["start"]


def prop_text(p: dict | None) -> str:
    """A database property value as one text cell ("" = empty)."""
    p = p or {}
    t = p.get("type", "")
    v = p.get(t)
    if v is None:
        return ""
    if t in ("title", "rich_text"):
        return _text(v)
    if t == "number":
        return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)
    if t == "checkbox":
        return "true" if v else "false"
    if t in ("select", "status"):
        return v.get("name", "")
    if t == "multi_select":
        return ", ".join(o.get("name", "") for o in v)
    if t == "date":
        return _date(v)
    if t in ("people",):
        return ", ".join(_user(u) for u in v)
    if t in ("created_by", "last_edited_by"):
        return _user(v)
    if t == "relation":
        return ", ".join(r.get("id", "") for r in v)
    if t == "files":
        return ", ".join(f.get("name", "") for f in v)
    if t == "unique_id":
        n = v.get("number")
        return "" if n is None else f"{v['prefix']}-{n}" if v.get("prefix") else str(n)
    if t == "formula":
        return prop_text({"type": v.get("type"), v.get("type", ""): v.get(v.get("type", ""))}) if v.get("type") else ""
    if t == "rollup":
        rt = v.get("type", "")
        if rt == "array":
            return ", ".join(x for x in (prop_text(e) for e in v.get("array") or []) if x)
        return prop_text({"type": rt, rt: v.get(rt)}) if rt else ""
    if t == "verification":
        return v.get("state", "") if isinstance(v, dict) else ""
    if isinstance(v, (str, int, float)):  # url, email, phone_number, created_time, last_edited_time
        return str(v)
    return ""


def database_title(db: dict) -> str:
    return _text(db.get("title")).strip()


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
        self._pages: list[dict] | None = None  # the listings, read once per instance (fetch and deleted share them)
        self._dbs: list[dict] | None = None

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

    def _search(self, kind: str) -> list[dict]:
        found: dict[str, dict] = {}
        body: dict = {"filter": {"property": "object", "value": kind}, "page_size": 100,
                      "sort": {"direction": "ascending", "timestamp": "last_edited_time"}}
        while True:
            out = self._json("POST", "/search", json=body)
            for p in out.get("results") or []:
                if p.get("object") == kind and not p.get("archived") and not p.get("in_trash"):
                    found[p["id"]] = p
            if len(found) > MAX_PAGES:
                raise NotionError(f"more than {MAX_PAGES} {kind}s are shared with the integration")
            if not out.get("has_more") or not out.get("next_cursor"):
                break
            body = {**body, "start_cursor": out["next_cursor"]}
        return sorted(found.values(), key=lambda p: (_when(p["last_edited_time"]), p["id"]))

    def pages(self) -> list[dict]:
        """Every page shared with the integration (not archived or trashed), oldest edit first."""
        if self._pages is None:
            self._pages = self._search("page")
        return self._pages

    def databases(self) -> list[dict]:
        """Every database shared with the integration (not archived or trashed), oldest edit first."""
        if self._dbs is None:
            self._dbs = self._search("database")
        return self._dbs

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

    # ------------------------------------------------------------ databases → datasets

    def _rows(self, db_id: str) -> list[dict] | None:
        """The database's live rows (pages), or None when it can't be read or is over the row cap."""
        rows: list[dict] = []
        body: dict = {"page_size": 100}
        while True:
            r = self._request("POST", f"/databases/{db_id}/query", json=body)
            if r.status_code in (403, 404):
                log.warning("notion database %s can't be read; skipped: %s (%s)", db_id, _reason(r), r.status_code)
                return None
            if r.status_code != 200:
                raise NotionError(f"Notion /databases/{db_id}/query failed ({r.status_code}): {_reason(r)}")
            out = r.json()
            rows += [p for p in out.get("results") or [] if not p.get("archived") and not p.get("in_trash")]
            if len(rows) > datasets.MAX_ROWS:
                log.warning("notion database %s has more than %d rows; skipped", db_id, datasets.MAX_ROWS)
                return None
            if not out.get("has_more") or not out.get("next_cursor"):
                break
            body = {**body, "start_cursor": out["next_cursor"]}
        return sorted(rows, key=lambda p: (p.get("created_time") or "", p["id"]))

    def snapshot(self, db: dict) -> DatasetRecord | None:
        """The database as a dataset snapshot: `page_id`, the title property, then the others by name."""
        props = db.get("properties") or {}
        title = [n for n, p in props.items() if p.get("type") == "title"]
        names = title + sorted((n for n in props if n not in title), key=lambda n: (n.lower(), n))
        rows = self._rows(db["id"])
        if rows is None:
            return None
        columns = ["page_id", *names]
        data = [[p["id"], *(prop_text((p.get("properties") or {}).get(n)) for n in names)] for p in rows]
        try:
            datasets.check_snapshot(columns, data)
        except ValueError as e:
            log.warning("notion database %s skipped: %s", db["id"], e)
            return None
        return DatasetRecord(key=db["id"], name=database_title(db) or f"notion {db['id']}", columns=columns,
                             rows=data)

    def _rows_hash(self) -> str:
        """Fingerprint of which databases and rows exist: a change re-reads every database (a row was deleted)."""
        h = hashlib.sha256()
        for d in sorted(d["id"] for d in self.databases()):
            h.update(f"d:{d}\n".encode())
        for p in sorted((p.get("parent") or {}).get("database_id", "") + "/" + p["id"] for p in self.pages()
                        if (p.get("parent") or {}).get("type") == "database_id"):
            h.update(f"r:{p}\n".encode())
        return h.hexdigest()[:16]

    def _changed_databases(self, since: datetime | None, rows_hash: str | None, new_hash: str) -> list[dict]:
        dbs = self.databases()
        if since is None or rows_hash != new_hash:
            return dbs
        edited = {(p.get("parent") or {}).get("database_id") for p in self.pages()
                  if (p.get("parent") or {}).get("type") == "database_id" and _when(p["last_edited_time"]) >= since}
        return [d for d in dbs if d["id"] in edited or _when(d["last_edited_time"]) >= since]

    # ------------------------------------------------------------ connector protocol

    def fetch(self, cursor: str | None) -> Iterator[Batch]:
        since, since_s, rows_hash = None, "", None
        if cursor and cursor.startswith(OLD_TAG + "edited:"):
            since_s = cursor.removeprefix(OLD_TAG + "edited:")
        elif cursor and cursor.startswith(TAG + "rows:") and ":edited:" in cursor:
            rows_hash, since_s = cursor.removeprefix(TAG + "rows:").split(":edited:", 1)
        if since_s:
            since = _when(since_s)
        new_hash = self._rows_hash()
        # Databases first, at the incoming cursor: a crash before the cursor moves re-reads them (idempotent).
        for db in self._changed_databases(since, rows_hash, new_hash):
            rec = self.snapshot(db)
            if rec is not None:
                yield Batch([], cursor, datasets=[rec])
        todo = [p for p in self.pages() if since is None or _when(p["last_edited_time"]) >= since]
        here = f"{TAG}rows:{new_hash}:edited:{since_s}"
        for i in range(0, len(todo), self.batch_size):
            chunk = todo[i:i + self.batch_size]
            records = []
            for p in chunk:
                body = self.content(p)
                if body is not None:
                    records.append(self._record(p, body))
            here = f"{TAG}rows:{new_hash}:edited:{chunk[-1]['last_edited_time']}"
            yield Batch(records, here)
        if not todo and here != cursor:
            yield Batch([], here)

    def deleted(self, known: set[str]) -> list[str]:
        """Synced pages no longer in the listing (archived, trashed, unshared). An empty listing reports nothing."""
        present = {p["id"] for p in self.pages()}
        if not present:
            if known:
                log.warning("no Notion pages are shared with the integration; not proposing %d deletions", len(known))
            return []
        return sorted(known - present)

    def deleted_datasets(self, known: set[str]) -> list[str]:
        """Synced databases no longer listed. Nothing listed at all (pages or databases) reports nothing."""
        present = {d["id"] for d in self.databases()}
        if not present and not self.pages():
            if known:
                log.warning("nothing is shared with the Notion integration; not proposing %d dataset deletions",
                            len(known))
            return []
        return sorted(known - present)


def _reason(r: httpx.Response) -> str:
    try:
        j = r.json()
        return f"{j.get('code', '')}: {j.get('message', '')}".strip(": ")
    except Exception:
        return r.text[:200]
