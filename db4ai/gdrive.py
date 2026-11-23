"""Google Drive / Docs connector on the connector framework (SPEC §6).

The owner supplies an OAuth token through the environment (`DB4AI_GDRIVE_TOKEN`, or a refresh token plus client
id/secret); DB4AI never runs an OAuth flow or stores the token. The connector sees whatever that account can read.

Cursor (opaque to the framework, never holds content):
* `list:<start>:<page>` while the first full listing is in progress: `<start>` is the changes start page token taken
  *before* listing, `<page>` the next `files.list` page. Changes made during the listing are picked up afterwards.
* `changes:<token>` afterwards: the Drive changes API page token.
* With folder scoping (`folders=` or `DB4AI_GDRIVE_FOLDERS`), the cursor is prefixed `scope:<hash>:`, a hash of the
  chosen folder ids, and the listing cursor is `list:<start>:<chunk>:<page>`. A cursor from another scope starts a
  fresh full listing, so adding a folder picks up its existing files.

Every cursor also carries `types:<hash>:`, a hash of the synced file types, so a release that syncs more types re-lists
once and picks up existing files of the new types (re-sending is idempotent).

Each API page becomes one batch, so a crash resumes at the last page saved. Synced files: Google Docs (exported as
plain text), plain text / Markdown files, and PDF / .docx files (text extracted with the file importers). A removed or trashed file is reported as a deletion, which proposes
archiving its item through the harness.

Google Sheets are synced as datasets, not items: each grid tab is read through the Sheets API (formatted values, first
row = header) and sent as a `DatasetRecord` keyed `<file id>#<sheet id>`, in its own batch at the cursor the page
started from. Tabs that disappear, and every tab of a removed spreadsheet, are reported in `deleted_datasets`.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Callable, Iterator

import httpx

from . import datasets
from .connectors import MAX_BATCH, Batch, DatasetRecord, Record
from .ingest import text_from_bytes

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/drive/v3"
SHEETS_API = "https://sheets.googleapis.com/v4"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GDOC = "application/vnd.google-apps.document"
FOLDER = "application/vnd.google-apps.folder"
SHEET = "application/vnd.google-apps.spreadsheet"
MAX_CELLS = 5_000_000  # a tab whose grid is larger is skipped (Sheets allows 10M cells per spreadsheet)
TABS_PER_QUERY = 50    # ranges per values:batchGet
MAX_FOLDERS = 5000  # a scope larger than this is refused rather than half-synced
PARENTS_PER_QUERY = 40  # `'<id>' in parents or ...` terms per files.list query
TEXT_TYPES = {"text/plain", "text/markdown", "text/x-markdown"}
PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
BINARY_TYPES = {PDF: ".pdf", DOCX: ".docx"}  # downloaded, then text extracted like a CLI import
SYNCED_TYPES = {GDOC, SHEET, *TEXT_TYPES, *BINARY_TYPES}
TYPES_TAG = "types:" + hashlib.sha256(",".join(sorted(SYNCED_TYPES)).encode()).hexdigest()[:8] + ":"
MAX_BYTES = 10 * 1024 * 1024  # Drive's own export limit; larger text files are skipped
FILE_FIELDS = "id,name,mimeType,modifiedTime,webViewLink,trashed,size,parents"
_ALL_DRIVES = {"supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}


class DriveError(Exception):
    """Drive refused a request that a retry won't fix (bad token, missing scope, bad cursor)."""


class TokenSource:
    """An access token from the environment, refreshed with a refresh token when one is configured."""

    def __init__(self, http: httpx.Client, access_token: str | None = None, refresh_token: str | None = None,
                 client_id: str | None = None, client_secret: str | None = None):
        self.http, self.token = http, access_token
        self.refresh_token, self.client_id, self.client_secret = refresh_token, client_id, client_secret
        if not self.token and not self.can_refresh:
            raise DriveError("set DB4AI_GDRIVE_TOKEN, or DB4AI_GDRIVE_REFRESH_TOKEN with DB4AI_GDRIVE_CLIENT_ID "
                             "and DB4AI_GDRIVE_CLIENT_SECRET")

    @classmethod
    def from_env(cls, http: httpx.Client) -> "TokenSource":
        e = lambda k: os.environ.get(k) or None  # noqa: E731
        return cls(http, e("DB4AI_GDRIVE_TOKEN"), e("DB4AI_GDRIVE_REFRESH_TOKEN"), e("DB4AI_GDRIVE_CLIENT_ID"),
                   e("DB4AI_GDRIVE_CLIENT_SECRET"))

    @property
    def can_refresh(self) -> bool:
        return bool(self.refresh_token and self.client_id and self.client_secret)

    def get(self) -> str:
        if not self.token:
            self.refresh()
        return self.token

    def refresh(self) -> None:
        if not self.can_refresh:
            raise DriveError("Drive token was rejected and no refresh token is configured")
        r = self.http.post(TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": self.refresh_token,
                                            "client_id": self.client_id, "client_secret": self.client_secret})
        if r.status_code != 200:
            raise DriveError(f"token refresh failed ({r.status_code})")
        self.token = r.json()["access_token"]


class GoogleDrive:
    def __init__(self, tokens: TokenSource | None = None, http: httpx.Client | None = None, page_size: int = 100,
                 retries: int = 4, sleep: Callable[[float], None] = time.sleep, folders: list[str] | None = None):
        self.http = http or httpx.Client(timeout=60)
        self.tokens = tokens or TokenSource.from_env(self.http)
        self.page_size, self.retries, self.sleep = page_size, retries, sleep
        if folders is None:
            folders = os.environ.get("DB4AI_GDRIVE_FOLDERS", "").split(",")
        # Folder (or shared drive) ids to sync, with everything below them. Empty = all the account can read.
        self.roots = sorted({f.strip() for f in folders if f and f.strip()})
        for f in self.roots:
            if "'" in f or "\\" in f:
                raise DriveError(f"not a Drive folder id: {f!r}")
        self._scope: dict[str, list[str]] | None = None  # folder id -> parent ids, resolved once per instance
        self._present: set[str] | None = None  # synced file ids in scope, listed once for the deletion checks
        # Dataset keys (`<file id>#<sheet id>`) DB4AI holds as live; set by connectors.run, kept current as we emit.
        self.known_datasets: set[str] = set()

    @property
    def scoped(self) -> bool:
        return bool(self.roots)

    @property
    def scope_tag(self) -> str:
        return "scope:" + hashlib.sha256(",".join(self.roots).encode()).hexdigest()[:12] + ":"

    @property
    def cursor_tag(self) -> str:
        """Prefix of every cursor: the folder scope (if any) and the synced file types. A cursor with another prefix
        starts a fresh full listing."""
        return (self.scope_tag if self.scoped else "") + TYPES_TAG

    # ------------------------------------------------------------ HTTP

    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        refreshed = False
        for attempt in range(self.retries + 1):
            r = self.http.get(path if path.startswith("https://") else f"{API}{path}", params=params,
                              headers={"Authorization": f"Bearer {self.tokens.get()}"})
            if r.status_code == 401 and not refreshed and self.tokens.can_refresh:
                self.tokens.refresh()
                refreshed = True
                continue
            if (r.status_code == 429 or r.status_code >= 500 or _rate_limited(r)) and attempt < self.retries:
                self.sleep(min(2 ** attempt, 32))
                continue
            return r
        return r

    def _json(self, path: str, params: dict | None = None) -> dict:
        r = self._get(path, params)
        if r.status_code != 200:
            raise DriveError(f"Drive {path} failed ({r.status_code}): {_reason(r)}")
        return r.json()

    def _content(self, f: dict) -> str | None:
        """Text of a synced file, or None when Drive won't hand it over (export disabled, deleted meanwhile)."""
        if f["mimeType"] == GDOC:
            r = self._get(f"/files/{f['id']}/export", {"mimeType": "text/plain"})
        else:
            r = self._get(f"/files/{f['id']}", {"alt": "media", "supportsAllDrives": "true"})
        if r.status_code in (403, 404):
            log.warning("skipping Drive file %s: %s (%s)", f["id"], _reason(r), r.status_code)
            return None
        if r.status_code != 200:
            raise DriveError(f"Drive download of {f['id']} failed ({r.status_code}): {_reason(r)}")
        ext = BINARY_TYPES.get(f["mimeType"])
        if ext:
            try:
                text = text_from_bytes(ext, r.content)
            except Exception as e:  # corrupt or encrypted file: skip it rather than block the sync
                log.warning("skipping Drive file %s: can't read %s (%s)", f["id"], ext, type(e).__name__)
                return None
        else:
            text = r.content.decode("utf-8", errors="replace")
        text = text.lstrip("\ufeff").replace("\r\n", "\n").strip()
        if not text and ext:  # e.g. a scanned PDF with no text layer (no OCR in v1)
            log.warning("skipping Drive file %s: no text in %s", f["id"], ext)
            return None
        return text

    # ------------------------------------------------------------ folder scope

    def folders(self) -> dict[str, list[str]]:
        """Every folder in scope (the roots and all folders below them) → its parents. Fail closed: a root that is
        missing, trashed or not a folder is an error, so a bad id can't look like "everything was deleted"."""
        if self._scope is not None:
            return self._scope
        scope: dict[str, list[str]] = {}
        for root in self.roots:
            r = self._get(f"/files/{root}", {"fields": "id,mimeType,trashed", "supportsAllDrives": "true"})
            if r.status_code != 200:
                raise DriveError(f"Drive folder {root} can't be read ({r.status_code}): {_reason(r)}")
            f = r.json()
            if f.get("mimeType") != FOLDER or f.get("trashed"):
                raise DriveError(f"Drive file {root} is not a folder (or is in the trash)")
            scope[root] = []
        frontier = list(self.roots)
        while frontier:
            found = []
            for f in self._query(f"mimeType = '{FOLDER}' and trashed = false", frontier, "id,parents"):
                if f["id"] not in scope:
                    scope[f["id"]] = f.get("parents") or []
                    found.append(f["id"])
            if len(scope) > MAX_FOLDERS:
                raise DriveError(f"Drive folder scope has more than {MAX_FOLDERS} folders; choose narrower folders")
            frontier = found
        self._scope = scope
        return scope

    def _in_scope(self, f: dict) -> bool:
        return not self.scoped or bool(set(f.get("parents") or []) & self.folders().keys())

    def _subtree(self, folder: str) -> list[str]:
        """`folder` and the in-scope folders below it."""
        scope, out, frontier = self.folders(), [folder], [folder]
        while frontier:
            frontier = [f for f, parents in scope.items() if set(parents) & set(frontier) and f not in out]
            out += frontier
        return out

    def _query(self, q: str, parents: list[str], fields: str) -> Iterator[dict]:
        """files.list for `q` restricted to children of `parents`, chunked so the query stays short."""
        for i in range(0, len(parents), PARENTS_PER_QUERY):
            yield from self._pages(_with_parents(q, parents[i:i + PARENTS_PER_QUERY]), fields)

    def _pages(self, q: str, fields: str, page: str | None = None) -> Iterator[dict]:
        while True:
            params = {"q": q, "pageSize": self.page_size, "corpora": "allDrives",
                      "fields": f"nextPageToken,files({fields})", **_ALL_DRIVES}
            if page:
                params["pageToken"] = page
            data = self._json("/files", params)
            yield from data.get("files", [])
            page = data.get("nextPageToken")
            if not page:
                return

    def deleted(self, known: set[str]) -> list[str]:
        """Scoped syncs only: synced files no longer anywhere in scope (moved out, or their folder was) are
        deletions. Changes to a file are already caught by the changes feed; a folder moving out isn't."""
        if not self.scoped or not known:
            return []
        present = self._present_ids()
        if not present:  # fail closed, as for transcripts: an empty scope reports nothing
            return []
        return sorted(known - present)

    def deleted_datasets(self, known: set[str]) -> list[str]:
        """Scoped syncs only: tabs of spreadsheets no longer anywhere in scope (as `deleted`, for datasets)."""
        if not self.scoped or not known:
            return []
        present = self._present_ids()
        if not present:
            return []
        return sorted(k for k in known if k.split("#", 1)[0] not in present)

    def _present_ids(self) -> set[str]:
        if self._present is None:
            self._present = {f["id"] for f in self._query(_SYNCED_Q, list(self.folders()), "id")}
        return self._present

    # ------------------------------------------------------------ records

    def _wanted(self, f: dict | None) -> bool:
        return bool(f) and not f.get("trashed") and f.get("mimeType") in SYNCED_TYPES \
            and int(f.get("size") or 0) <= MAX_BYTES and self._in_scope(f)

    def _records(self, files: list[dict]) -> list[Record]:
        out, seen = [], set()
        for f in files:
            if not self._wanted(f) or f["id"] in seen or f["mimeType"] == SHEET:
                continue
            seen.add(f["id"])
            body = self._content(f)
            if body is None:
                continue
            meta = {"mime_type": f["mimeType"], "modified_time": f.get("modifiedTime")}
            if f.get("webViewLink"):
                meta["web_view_link"] = f["webViewLink"]
            out.append(Record(key=f["id"], type="document", title=f.get("name") or f["id"], body=body,
                              metadata=meta))
        return out

    # ------------------------------------------------------------ spreadsheets → datasets

    def _tab_keys(self, fid: str) -> set[str]:
        return {k for k in self.known_datasets if k.startswith(fid + "#")}

    def _gone(self, fids: list[str]) -> list[str]:
        """Dataset keys of removed spreadsheets (every tab DB4AI holds for them)."""
        out = sorted(k for fid in fids for k in self._tab_keys(fid))
        self.known_datasets.difference_update(out)
        return out

    def _tabs(self, f: dict) -> tuple[list[DatasetRecord], set[str]] | None:
        """Snapshots of a spreadsheet's grid tabs, plus the keys of every grid tab it has (including skipped ones),
        or None when the Sheets API won't hand it over (then nothing is reported deleted)."""
        fid, base = f["id"], f.get("name") or f["id"]
        r = self._get(f"{SHEETS_API}/spreadsheets/{fid}",
                      {"fields": "sheets.properties(sheetId,title,sheetType,gridProperties(rowCount,columnCount))"})
        if r.status_code in (403, 404):
            log.warning("skipping Drive spreadsheet %s: %s (%s)", fid, _reason(r), r.status_code)
            return None
        if r.status_code != 200:
            raise DriveError(f"Sheets read of {fid} failed ({r.status_code}): {_reason(r)}")
        grid = [s["properties"] for s in r.json().get("sheets", [])
                if s.get("properties", {}).get("sheetType", "GRID") == "GRID"]
        present = {f"{fid}#{p['sheetId']}" for p in grid}
        wanted = []
        for p in grid:
            g = p.get("gridProperties") or {}
            if int(g.get("rowCount") or 0) * int(g.get("columnCount") or 0) > MAX_CELLS:
                log.warning("skipping tab %s of Drive spreadsheet %s: more than %d cells", p["sheetId"], fid, MAX_CELLS)
            else:
                wanted.append(p)
        values: list[list] = []
        for i in range(0, len(wanted), TABS_PER_QUERY):
            ranges = ["'" + str(p.get("title", "")).replace("'", "''") + "'" for p in wanted[i:i + TABS_PER_QUERY]]
            r = self._get(f"{SHEETS_API}/spreadsheets/{fid}/values:batchGet",
                          {"ranges": ranges, "valueRenderOption": "FORMATTED_VALUE", "majorDimension": "ROWS"})
            if r.status_code in (403, 404):
                log.warning("skipping Drive spreadsheet %s: %s (%s)", fid, _reason(r), r.status_code)
                return None
            if r.status_code != 200:
                raise DriveError(f"Sheets read of {fid} failed ({r.status_code}): {_reason(r)}")
            values += [vr.get("values") or [] for vr in r.json().get("valueRanges", [])]
        if len(values) != len(wanted):
            raise DriveError(f"Sheets returned {len(values)} ranges for {len(wanted)} tabs of {fid}")
        out = []
        for p, rows in zip(wanted, values):
            rows = [["" if c is None else str(c) for c in row] for row in rows]
            rows = [row for row in rows if any(c.strip() for c in row)]  # blank spacer rows aren't data
            if not rows:
                continue
            width = max(len(row) for row in rows)
            header = rows[0] + [""] * (width - len(rows[0]))  # a column with values but no header is still data
            try:
                datasets.check_snapshot(header, rows[1:])
            except ValueError as e:
                log.warning("skipping tab %s of Drive spreadsheet %s: %s", p["sheetId"], fid, e)
                continue
            name = base if len(grid) == 1 else f"{base} {p.get('title') or p['sheetId']}"
            out.append(DatasetRecord(key=f"{fid}#{p['sheetId']}", name=name, columns=header, rows=rows[1:]))
        return out, present

    def _sheet_batches(self, files: list[dict], here: str) -> Iterator[Batch]:
        """One or more batches per changed spreadsheet, all at `here` (the cursor the page started from), so a crash
        replays the page and a page of large spreadsheets never becomes one huge transaction."""
        seen = set()
        for f in files:
            if not f or f.get("mimeType") != SHEET or not self._wanted(f) or f["id"] in seen:
                continue
            seen.add(f["id"])
            tabs = self._tabs(f)
            if tabs is None:
                continue
            recs, present = tabs
            gone = sorted(self._tab_keys(f["id"]) - present)
            self.known_datasets.difference_update(gone)
            self.known_datasets.update(r.key for r in recs)
            for i in range(0, len(recs), MAX_BATCH):
                yield Batch([], here, datasets=recs[i:i + MAX_BATCH])
            for i in range(0, len(gone), MAX_BATCH):
                yield Batch([], here, deleted_datasets=gone[i:i + MAX_BATCH])

    # ------------------------------------------------------------ sync

    def fetch(self, cursor: str | None) -> Iterator[Batch]:
        tag = self.cursor_tag
        if cursor and not cursor.startswith(tag):
            if not cursor.startswith(("scope:", "types:", "list:", "changes:")):
                raise DriveError(f"not a Google Drive cursor: {cursor!r}")
            log.info("Drive folder scope or synced file types changed: starting a full listing")
            cursor = None
        inner = cursor[len(tag):] if cursor else None
        for b in self._fetch(inner):
            yield Batch(b.records, tag + b.cursor, b.deleted, b.datasets, b.deleted_datasets)

    def _fetch(self, cursor: str | None) -> Iterator[Batch]:
        if not cursor:
            start = self._json("/changes/startPageToken", {"supportsAllDrives": "true"})["startPageToken"]
            yield from self._list(start, None)
            cursor = f"changes:{start}"
        elif cursor.startswith("list:"):
            _, start, page = cursor.split(":", 2)
            yield from self._list(start, page)
            cursor = f"changes:{start}"
        elif not cursor.startswith("changes:"):
            raise DriveError(f"not a Google Drive cursor: {cursor!r}")
        yield from self._changes(cursor.split(":", 1)[1])

    def _list(self, start: str, page: str | None) -> Iterator[Batch]:
        if self.scoped:
            yield from self._list_scoped(start, page)
            return
        while True:
            params = {"q": _SYNCED_Q, "pageSize": self.page_size, "corpora": "allDrives", "orderBy": "modifiedTime",
                      "fields": f"nextPageToken,files({FILE_FIELDS})", **_ALL_DRIVES}
            if page:
                params["pageToken"] = page
            data = self._json("/files", params)
            yield from self._sheet_batches(data.get("files", []), f"list:{start}:{page or ''}")
            page = data.get("nextPageToken")
            records = self._records(data.get("files", []))
            if not page:
                yield Batch(records, f"changes:{start}")
                return
            yield Batch(records, f"list:{start}:{page}")

    def _list_scoped(self, start: str, resume: str | None) -> Iterator[Batch]:
        """One files.list per chunk of in-scope folders; cursor `list:<start>:<chunk>:<page>`. Folders that change
        while listing are replayed by the changes feed from `<start>`."""
        folders = sorted(self.folders())
        chunks = [folders[i:i + PARENTS_PER_QUERY] for i in range(0, len(folders), PARENTS_PER_QUERY)]
        chunk, page = 0, None
        if resume:
            c, _, p = resume.partition(":")
            chunk, page = int(c), p or None
        for i in range(chunk, len(chunks)):
            q = _with_parents(_SYNCED_Q, chunks[i])
            while True:
                params = {"q": q, "pageSize": self.page_size, "corpora": "allDrives",
                          "fields": f"nextPageToken,files({FILE_FIELDS})", **_ALL_DRIVES}
                if page:
                    params["pageToken"] = page
                data = self._json("/files", params)
                yield from self._sheet_batches(data.get("files", []), f"list:{start}:{i}:{page or ''}")
                page = data.get("nextPageToken")
                records = self._records(data.get("files", []))
                if page:
                    yield Batch(records, f"list:{start}:{i}:{page}")
                    continue
                if i + 1 < len(chunks):
                    yield Batch(records, f"list:{start}:{i + 1}:")
                else:
                    yield Batch(records, f"changes:{start}")
                break

    def _moved_in(self, folder: str, cursor: str) -> Iterator[Batch]:
        """A folder that moved into scope (or was created there with files): its existing files don't show up in
        the changes feed, so list them. The cursor doesn't move; re-sending is idempotent."""
        files = list(self._query(_SYNCED_Q, self._subtree(folder), FILE_FIELDS))
        for i in range(0, len(files), self.page_size):
            yield from self._sheet_batches(files[i:i + self.page_size], cursor)
            records = self._records(files[i:i + self.page_size])
            if records:
                yield Batch(records, cursor)

    def _changes(self, token: str) -> Iterator[Batch]:
        while True:
            data = self._json("/changes", {
                "pageToken": token, "pageSize": self.page_size, "includeRemoved": "true", "spaces": "drive",
                "fields": f"nextPageToken,newStartPageToken,changes(fileId,removed,file({FILE_FIELDS}))",
                **_ALL_DRIVES})
            changes = data.get("changes", [])
            # Removed (deleted, or the account lost access) or trashed: DB4AI can no longer read it from Drive, so
            # the framework proposes archiving the item (ask_first). The last change to a file in the page wins.
            # With a folder scope, a file now outside it counts as removed too (unknown keys are ignored).
            last = {c.get("fileId") or (c.get("file") or {}).get("id"): c for c in changes}
            deleted = [fid for fid, c in last.items()
                       if fid and (c.get("removed") or (c.get("file") or {}).get("trashed")
                                   or (self.scoped and (c.get("file") or {}).get("mimeType") in SYNCED_TYPES
                                       and not self._in_scope(c["file"])))]
            if self.scoped:
                for fid, c in last.items():
                    f = c.get("file") or {}
                    if f.get("mimeType") == FOLDER and not f.get("trashed") and fid in self.folders():
                        yield from self._moved_in(fid, f"changes:{token}")
            changed = [c["file"] for fid, c in last.items() if fid not in deleted and c.get("file")]
            yield from self._sheet_batches(changed, f"changes:{token}")
            records = self._records(changed)
            gone = self._gone(deleted)
            nxt = data.get("nextPageToken")
            new_start = data.get("newStartPageToken")
            if nxt:
                yield Batch(records, f"changes:{nxt}", deleted, deleted_datasets=gone)
                token = nxt
                continue
            # Last page: only yield if something changed or the token moved, so an idle sync writes nothing.
            if records or deleted or gone or new_start != token:
                yield Batch(records, f"changes:{new_start or token}", deleted, deleted_datasets=gone)
            return


_SYNCED_Q = "trashed = false and (" + " or ".join(f"mimeType = '{m}'" for m in sorted(SYNCED_TYPES)) + ")"


def _with_parents(q: str, parents: list[str]) -> str:
    return f"{q} and (" + " or ".join(f"'{p}' in parents" for p in parents) + ")"


def _reason(r: httpx.Response) -> str:
    try:
        err = r.json().get("error", {})
        errs = err.get("errors") or [{}]
        return errs[0].get("reason") or err.get("message") or r.reason_phrase
    except Exception:
        return r.reason_phrase


def _rate_limited(r: httpx.Response) -> bool:
    return r.status_code == 403 and _reason(r) in {"rateLimitExceeded", "userRateLimitExceeded"}
