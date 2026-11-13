"""Content-addressed blob store (SPEC §2).

Write order gives atomicity without 2PC: the blob is stored under its sha256 *before*
the Postgres transaction that references it commits. A failed commit only leaves an
orphan blob, which `gc_orphans` removes later. A row never points at a missing blob.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from .config import settings
from .db import Conn


class BlobStore(Protocol):
    def put(self, sha256: str, data: bytes) -> None: ...
    def get(self, sha256: str) -> bytes: ...
    def exists(self, sha256: str) -> bool: ...
    def delete(self, sha256: str) -> None: ...
    def list(self, older_than: float = 0) -> list[str]: ...


class FileBlobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, sha: str) -> Path:
        return self.root / sha[:2] / sha

    def put(self, sha256: str, data: bytes) -> None:
        p = self._path(sha256)
        if p.exists():
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)  # atomic rename: readers never see a partial blob

    def get(self, sha256: str) -> bytes:
        return self._path(sha256).read_bytes()

    def exists(self, sha256: str) -> bool:
        return self._path(sha256).exists()

    def delete(self, sha256: str) -> None:
        self._path(sha256).unlink(missing_ok=True)

    def list(self, older_than: float = 0) -> list[str]:
        import time

        cutoff = time.time() - older_than
        return [p.name for p in self.root.glob("*/*")
                if not p.name.endswith(".tmp") and p.stat().st_mtime <= cutoff]


class S3BlobStore:
    def __init__(self, bucket: str, prefix: str = ""):
        import boto3  # optional dependency: pip install db4ai[s3]

        self.s3 = boto3.client("s3", endpoint_url=os.environ.get("DB4AI_S3_ENDPOINT"))
        self.bucket, self.prefix = bucket, prefix.strip("/")

    def _key(self, sha: str) -> str:
        return f"{self.prefix}/{sha}" if self.prefix else sha

    def put(self, sha256: str, data: bytes) -> None:
        if not self.exists(sha256):
            self.s3.put_object(Bucket=self.bucket, Key=self._key(sha256), Body=data)

    def get(self, sha256: str) -> bytes:
        return self.s3.get_object(Bucket=self.bucket, Key=self._key(sha256))["Body"].read()

    def exists(self, sha256: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=self._key(sha256))
            return True
        except Exception:
            return False

    def delete(self, sha256: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=self._key(sha256))

    def list(self, older_than: float = 0) -> list[str]:
        import datetime as dt

        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=older_than)
        out: list[str] = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=self.prefix):
            out += [o["Key"].rsplit("/", 1)[-1] for o in page.get("Contents", []) if o["LastModified"] <= cutoff]
        return out


def open_store(url: str | None = None) -> BlobStore:
    u = urlparse(url or settings().blob_url)
    if u.scheme == "file":
        return FileBlobStore((u.netloc + u.path) or ".db4ai-blobs")
    if u.scheme == "s3":
        return S3BlobStore(u.netloc, u.path)
    raise ValueError(f"unsupported blob url {url!r}")


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def store_blob(conn: Conn, store: BlobStore, data: bytes, content_type: str | None = None) -> str:
    """Put the blob first, then register it in the caller's transaction."""
    sha = sha256_of(data)
    store.put(sha, data)
    conn.execute(
        "insert into blobs (sha256, size_bytes, content_type) values (%s, %s, %s) on conflict do nothing",
        (sha, len(data), content_type),
    )
    return sha


def gc_orphans(conn: Conn, store: BlobStore, grace_seconds: float = 3600) -> list[str]:
    """Delete stored blobs that no committed row references.

    The grace period protects blobs whose referencing transaction is still in flight.
    """
    candidates = store.list(older_than=grace_seconds)
    known = {r["sha256"] for r in conn.execute("select sha256 from blobs")}
    removed = [sha for sha in candidates if sha not in known]
    for sha in removed:
        store.delete(sha)
    return removed
