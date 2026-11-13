"""Push-based ingestion (SPEC §6): files, folders and Markdown/Obsidian memory vaults."""

from __future__ import annotations

import io
import mimetypes
import re
from pathlib import Path
from uuid import UUID

import yaml

from . import datasets, store
from .auth import Actor
from .blobs import BlobStore, store_blob
from .db import Conn, Provenance, transaction

TEXT_EXT = {".md", ".markdown", ".txt"}
DOC_EXT = {".pdf", ".docx"}
TABLE_EXT = {".csv"}
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)
_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
_TAG = re.compile(r"(?:^|\s)#([A-Za-z][\w/-]+)")


def parse_markdown(text: str) -> tuple[dict, str]:
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), text[m.end():]


def _jsonable(v):
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)  # dates etc.


def extract_text(path: Path, data: bytes) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader

        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
    if ext == ".docx":
        import docx

        return "\n".join(p.text for p in docx.Document(io.BytesIO(data)).paragraphs)
    return data.decode("utf-8", errors="replace")


def _title(path: Path, meta: dict, body: str) -> str:
    if meta.get("title"):
        return str(meta["title"])
    h = re.search(r"^#\s+(.+)$", body, re.M)
    return h.group(1).strip() if h else path.stem


def import_file(conn: Conn, actor: Actor, team_id: UUID, path: Path, *, blobs: BlobStore,
                item_type: str = "document", root: Path | None = None, source: str = "file-import") -> dict:
    path = Path(path)
    rel = str(path.relative_to(root)) if root else path.name
    data = path.read_bytes()
    ext = path.suffix.lower()
    prov = Provenance(actor.id, source, f"import {rel}")
    if ext in TABLE_EXT:
        with transaction(conn, prov):
            return datasets.import_csv(conn, actor, prov, team_id, path.stem, data.decode("utf-8-sig"))
    text = extract_text(path, data)
    meta, body = parse_markdown(text) if ext in TEXT_EXT else ({}, text)
    meta = _jsonable(meta)
    if ext in TEXT_EXT:
        tags = set(meta.get("tags", []) if isinstance(meta.get("tags"), list) else [])
        tags |= set(_TAG.findall(body))
        if tags:
            meta["tags"] = sorted(tags)
    meta["path"] = rel
    item_type = str(meta.pop("type", item_type)) if isinstance(meta.get("type"), str) else item_type
    with transaction(conn, prov):
        blob_sha = None
        if ext in DOC_EXT:  # blob is written first; its row commits with the item (SPEC §2)
            blob_sha = store_blob(conn, blobs, data, mimetypes.guess_type(path.name)[0])
        r = store.save_item(conn, actor, prov, team_id=team_id, type=item_type, title=_title(path, meta, body),
                            body=body, metadata=meta, external_key=rel, blob_sha256=blob_sha)
    return {"path": rel, "item_id": str(r["item_id"]), "version": r["version"], "changed": r["changed"],
            "wikilinks": _WIKILINK.findall(body) if ext in TEXT_EXT else []}


def import_folder(conn: Conn, actor: Actor, team_id: UUID, root: Path, *, blobs: BlobStore,
                  item_type: str = "document", source: str = "folder-import") -> dict:
    root = Path(root)
    results = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or any(part.startswith(".") for part in p.relative_to(root).parts):
            continue
        if p.suffix.lower() not in TEXT_EXT | DOC_EXT | TABLE_EXT:
            continue
        results.append(import_file(conn, actor, team_id, p, blobs=blobs, item_type=item_type, root=root, source=source))
    # Resolve [[wikilinks]] once every note exists.
    by_name = {Path(r["path"]).stem.lower(): r["item_id"] for r in results if "item_id" in r}
    linked = 0
    prov = Provenance(actor.id, source, "obsidian wikilink")
    with transaction(conn, prov):
        for r in results:
            for target in r.get("wikilinks", []):
                dst = by_name.get(Path(target.strip()).stem.lower())
                if dst and store.add_link(conn, prov, UUID(r["item_id"]), UUID(dst), "wikilink", 1.0):
                    linked += 1
    return {"files": len(results), "changed": sum(1 for r in results if r.get("changed", True)),
            "wikilinks": linked, "results": results}


def import_vault(conn: Conn, actor: Actor, team_id: UUID, root: Path, *, blobs: BlobStore) -> dict:
    """Team memory repo (Markdown/Obsidian): every note becomes a `memory` item."""
    return import_folder(conn, actor, team_id, root, blobs=blobs, item_type="memory", source="memory-vault")
