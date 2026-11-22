"""Google Drive / Docs connector on the connector framework (SPEC §6).

The owner supplies an OAuth token through the environment (`DB4AI_GDRIVE_TOKEN`, or a refresh token plus client
id/secret); DB4AI never runs an OAuth flow or stores the token. The connector sees whatever that account can read.

Cursor (opaque to the framework, never holds content):
* `list:<start>:<page>` while the first full listing is in progress: `<start>` is the changes start page token taken
  *before* listing, `<page>` the next `files.list` page. Changes made during the listing are picked up afterwards.
* `changes:<token>` afterwards: the Drive changes API page token.

Each API page becomes one batch, so a crash resumes at the last page saved. Synced files: Google Docs (exported as
plain text) and plain text / Markdown files. Removed and trashed files are skipped in v1 (see ROADMAP
"Source deletions in connectors").
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable, Iterator

import httpx

from .connectors import Batch, Record

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/drive/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GDOC = "application/vnd.google-apps.document"
TEXT_TYPES = {"text/plain", "text/markdown", "text/x-markdown"}
SYNCED_TYPES = {GDOC, *TEXT_TYPES}
MAX_BYTES = 10 * 1024 * 1024  # Drive's own export limit; larger text files are skipped
FILE_FIELDS = "id,name,mimeType,modifiedTime,webViewLink,trashed,size"
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
                 retries: int = 4, sleep: Callable[[float], None] = time.sleep):
        self.http = http or httpx.Client(timeout=60)
        self.tokens = tokens or TokenSource.from_env(self.http)
        self.page_size, self.retries, self.sleep = page_size, retries, sleep

    # ------------------------------------------------------------ HTTP

    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        refreshed = False
        for attempt in range(self.retries + 1):
            r = self.http.get(f"{API}{path}", params=params,
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
        return r.content.decode("utf-8", errors="replace").lstrip("﻿").replace("\r\n", "\n").strip()

    # ------------------------------------------------------------ records

    def _wanted(self, f: dict | None) -> bool:
        return bool(f) and not f.get("trashed") and f.get("mimeType") in SYNCED_TYPES \
            and int(f.get("size") or 0) <= MAX_BYTES

    def _records(self, files: list[dict]) -> list[Record]:
        out, seen = [], set()
        for f in files:
            if not self._wanted(f) or f["id"] in seen:
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

    # ------------------------------------------------------------ sync

    def fetch(self, cursor: str | None) -> Iterator[Batch]:
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
        q = "trashed = false and (" + " or ".join(f"mimeType = '{m}'" for m in sorted(SYNCED_TYPES)) + ")"
        while True:
            params = {"q": q, "pageSize": self.page_size, "corpora": "allDrives", "orderBy": "modifiedTime",
                      "fields": f"nextPageToken,files({FILE_FIELDS})", **_ALL_DRIVES}
            if page:
                params["pageToken"] = page
            data = self._json("/files", params)
            page = data.get("nextPageToken")
            records = self._records(data.get("files", []))
            if not page:
                yield Batch(records, f"changes:{start}")
                return
            yield Batch(records, f"list:{start}:{page}")

    def _changes(self, token: str) -> Iterator[Batch]:
        while True:
            data = self._json("/changes", {
                "pageToken": token, "pageSize": self.page_size, "includeRemoved": "true", "spaces": "drive",
                "fields": f"nextPageToken,newStartPageToken,changes(fileId,removed,file({FILE_FIELDS}))",
                **_ALL_DRIVES})
            files = [c.get("file") for c in data.get("changes", []) if not c.get("removed")]
            records = self._records([f for f in files if f])
            nxt = data.get("nextPageToken")
            new_start = data.get("newStartPageToken")
            if nxt:
                yield Batch(records, f"changes:{nxt}")
                token = nxt
                continue
            # Last page: only yield if something changed or the token moved, so an idle sync writes nothing.
            if records or new_start != token:
                yield Batch(records, f"changes:{new_start or token}")
            return


def _reason(r: httpx.Response) -> str:
    try:
        err = r.json().get("error", {})
        errs = err.get("errors") or [{}]
        return errs[0].get("reason") or err.get("message") or r.reason_phrase
    except Exception:
        return r.reason_phrase


def _rate_limited(r: httpx.Response) -> bool:
    return r.status_code == 403 and _reason(r) in {"rateLimitExceeded", "userRateLimitExceeded"}
