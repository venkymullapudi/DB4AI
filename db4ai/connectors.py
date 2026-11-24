"""Connector framework: incremental sync from external sources (SPEC §6).

A connector turns a source (a folder of transcripts, later Google Drive...) into batches of `Record`s plus the
cursor reached after each batch. `apply_batch` saves a batch the same way `POST /items` saves one item and moves
the source's cursor in the same transaction, with compare-and-set on the previous cursor, so a crash never skips
records and two syncers of one source can't interleave. Re-sending a record is harmless: identical content adds
no version. HTTP connectors that run elsewhere use the same function through `POST /sync/{source}`.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Protocol
from uuid import UUID

from . import datasets, harness, store
from .auth import Actor
from .db import Conn, Provenance, log_change, transaction

log = logging.getLogger(__name__)

MAX_BATCH = 500
LIVE = ("active", "stale")  # statuses a source deletion proposes to archive
SOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


class CursorConflict(Exception):
    """The stored cursor isn't the one the batch was fetched from (another sync ran in between)."""


@dataclass
class Record:
    key: str                 # stable id in the source; becomes external_key "<source>:<key>"
    type: str
    title: str
    body: str = ""
    metadata: dict = field(default_factory=dict)
    # Actor names who may read the item in the source (e.g. a file's permissions). None = the source says nothing
    # (the whole team, or the item's current access stays). Names that aren't members of the team are dropped:
    # access only narrows. See `_sync_readers`.
    readers: list[str] | None = None
    # The same, as email addresses (matched to `actors.email`), for sources that only know emails (e.g. Drive).
    # The record says something about access when either list is set.
    reader_emails: list[str] | None = None


@dataclass
class DatasetRecord:
    """A full snapshot of one table in the source (e.g. a sheet tab): header + rows of cell strings."""
    key: str                 # stable id in the source; becomes datasets.external_key "<source>:<key>"
    name: str                # dataset name on first sync (table data.<team>_<name>); later renames are ignored
    columns: list[str]
    rows: list[list] = field(default_factory=list)


@dataclass
class Batch:
    records: list[Record]
    cursor: str | None       # where the source is once these records are saved
    deleted: list[str] = field(default_factory=list)  # keys the source deleted → propose archive_item
    datasets: list[DatasetRecord] = field(default_factory=list)
    deleted_datasets: list[str] = field(default_factory=list)  # → propose archive_dataset


class Connector(Protocol):
    def fetch(self, cursor: str | None) -> Iterator[Batch]: ...

    # Optional, for sources whose cursor can't see deletions (e.g. a folder): given the keys DB4AI holds as live,
    # return those the source no longer has. Called by `run` after the batches.
    # def deleted(self, known: set[str]) -> list[str]: ...
    # Optional, for connectors that emit dataset records: `run` sets `known_datasets` (live dataset keys of the
    # source) before fetching, so the connector can report tabs/files that went away; and calls
    # `deleted_datasets(known)` after the batches, like `deleted`.
    # known_datasets: set[str]
    # def deleted_datasets(self, known: set[str]) -> list[str]: ...


def _check_source(source: str) -> str:
    if not SOURCE_RE.match(source or ""):
        raise ValueError("source must be 1-64 chars of a-z, 0-9, '_', '.', '-' (starting with a letter or digit)")
    return source


def get_cursor(conn: Conn, actor: Actor, source: str, team_id: UUID) -> dict:
    actor.require_write(team_id)
    row = conn.execute("select cursor, batches, records, updated_at from sync_cursors where source=%s and team_id=%s",
                       (_check_source(source), team_id)).fetchone()
    return {"source": source, "team_id": team_id, "cursor": row["cursor"] if row else None,
            "batches": row["batches"] if row else 0, "records": row["records"] if row else 0,
            "updated_at": row["updated_at"] if row else None}


def _withdraw_deletions(conn: Conn, prov: Provenance, source: str, item_id: UUID) -> None:
    """The record is back in the source: a pending proposal to archive it as deleted must not be approved later."""
    rows = conn.execute(
        "update harness_decisions set status='expired', decision_note=%s"
        " where status='pending' and action_type='archive_item' and payload->>'item_id'=%s"
        " and payload->>'source_deleted'=%s returning id",
        (f"withdrawn: back in {source}", str(item_id), source)).fetchall()
    for r in rows:
        log_change(conn, prov, "harness", str(r["id"]), "withdraw", after={"reason": f"back in {source}"})


def _propose_deletion(conn: Conn, actor: Actor, source: str, team_id: UUID, key: str) -> dict | None:
    cur = conn.execute("select id, version, status from items_current where team_id=%s and external_key=%s",
                       (team_id, f"{source}:{key}")).fetchall()
    out = None
    for row in cur:  # external_key is unique per (team, type); a key could exist under more than one type
        if row["status"] not in LIVE:
            continue
        d = harness.propose(conn, actor, "archive_item",
                            {"item_id": str(row["id"]), "for_version": row["version"], "source_deleted": source},
                            reason=f"deleted in {source}", source=f"connector:{source}")
        out = {"key": key, "item_id": row["id"], "decision_id": d["decision_id"], "status": d["status"]}
    return out


def _team_readers(conn: Conn, team_id: UUID, names: list[str] | None,
                  emails: list[str] | None = None) -> tuple[list[UUID], list[str]]:
    """Resolve reader names and emails to members of the team; the rest (unknown actors or emails, other teams) are
    returned as dropped."""
    names = sorted(set(names or []))
    emails = sorted({e.strip().lower() for e in emails or []})
    rows = conn.execute("select a.id, a.name, a.email from actors a join team_members m on m.actor_id = a.id"
                        " where m.team_id = %s and (a.name = any(%s) or a.email = any(%s))",
                        (team_id, names, emails)).fetchall()
    by_name = {r["name"]: r["id"] for r in rows}
    by_email = {r["email"]: r["id"] for r in rows if r["email"]}
    ids = {by_name[n] for n in names if n in by_name} | {by_email[e] for e in emails if e in by_email}
    dropped = [n for n in names if n not in by_name] + [e for e in emails if e not in by_email]
    return sorted(ids, key=str), dropped


def _sync_readers(conn: Conn, actor: Actor, prov: Provenance, source: str, item_id: UUID,
                  readers: list[UUID]) -> dict | None:
    """The source's readers of an existing item differ from its ACL: propose `set_item_access` (always ask_first).

    The syncing actor stays a reader so the next sync can still write the item. The payload names the ACL state it
    was computed from (`acl_changes`), so a re-sent batch is the same decision while a later, real change in the
    source is a new one. An older pending proposal from this source is withdrawn when the source moves again (or
    back to what the item already has)."""
    want = sorted({*readers, actor.id}, key=str)
    have = store.item_readers(conn, item_id)
    acl_changes = conn.execute("select count(*) as n from change_log where entity='item_acl' and entity_id=%s",
                               (str(item_id),)).fetchone()["n"]
    payload = {"item_id": str(item_id), "readers": [str(r) for r in want], "source_readers": source,
               "acl_changes": acl_changes}
    fp = harness.fingerprint("set_item_access", payload)
    if have is not None and set(have) == set(want):
        fp = None  # nothing to change: withdraw anything pending from this source
    for r in conn.execute(
            "update harness_decisions set status='expired', decision_note=%s"
            " where status='pending' and action_type='set_item_access' and payload->>'item_id'=%s"
            " and payload->>'source_readers'=%s and fingerprint is distinct from %s::text returning id",
            (f"superseded by a newer sync from {source}", str(item_id), source, fp)).fetchall():
        log_change(conn, prov, "harness", str(r["id"]), "withdraw", after={"reason": "superseded"})
    if fp is None:
        return None
    d = harness.propose(conn, actor, "set_item_access", payload,
                        reason=f"readers changed in {source}", source=f"connector:{source}")
    return {"decision_id": d["decision_id"], "status": d["status"]}


def _dataset_by_key(conn: Conn, team_id: UUID, external_key: str, lock: bool = False) -> dict | None:
    return conn.execute("select * from datasets where team_id=%s and external_key=%s" + (" for update" if lock else ""),
                        (team_id, external_key)).fetchone()


def _sync_dataset(conn: Conn, actor: Actor, prov: Provenance, source: str, team_id: UUID,
                  rec: DatasetRecord) -> dict:
    """Apply one snapshot. Schema drift (new, removed or retyped columns) is proposed as `alter_dataset` and the
    snapshot waits (fail closed) until it is applied, so the table never holds a half-synced mix."""
    ext = f"{source}:{rec.key}"
    header = datasets.snapshot_header(rec.columns)
    ds = _dataset_by_key(conn, team_id, ext, lock=True)
    out = {"key": rec.key}
    if ds is None:
        name = datasets.slug(rec.name or rec.key)
        if datasets.get_dataset(conn, team_id, name) is not None:  # name taken (another sheet, a CSV import...)
            name = f"{name[:40]}_{hashlib.sha256(ext.encode()).hexdigest()[:8]}"
        cols = [{"name": h, "type": datasets._infer([r[i] for r in rec.rows if i < len(r) and r[i] is not None])}
                for i, h in enumerate(header)]
        ds = datasets.create_dataset(conn, actor, prov, team_id, name, cols)
        conn.execute("update datasets set external_key=%s where id=%s", (ext, ds["id"]))
        ds = _dataset_by_key(conn, team_id, ext, lock=True)
        out["created"] = True
    out.update(dataset_id=ds["id"], table=f"data.{ds['table_name']}")
    if ds["archived_at"] is not None:  # an approved archive stays archived; a human can revert the decision
        return {**out, "status": "archived", "inserted": 0, "deleted": 0}
    _withdraw_dataset_deletions(conn, prov, source, ds["id"])
    change = datasets.drift(ds, header, rec.rows)
    if any(change.values()):
        payload = {"dataset_id": str(ds["id"]), **change}
        for r in conn.execute(  # a newer drift supersedes an older pending one for this dataset
                "update harness_decisions set status='expired', decision_note='superseded by a newer snapshot'"
                " where status='pending' and action_type='alter_dataset' and payload->>'dataset_id'=%s"
                " and fingerprint <> %s returning id",
                (str(ds["id"]), harness.fingerprint("alter_dataset", payload))).fetchall():
            log_change(conn, prov, "harness", str(r["id"]), "withdraw", after={"reason": "superseded"})
        d = harness.propose(conn, actor, "alter_dataset", payload,
                            reason=f"columns changed in {source}", source=f"connector:{source}")
        out["decision_id"], out["decision_status"] = d["decision_id"], d["status"]
        if d["status"] != "applied_auto":
            return {**out, "status": "schema_drift", "drift": change, "inserted": 0, "deleted": 0}
        ds = _dataset_by_key(conn, team_id, ext, lock=True)
    return {**out, "status": "synced", **datasets.apply_snapshot(conn, ds, header, rec.rows)}


def _withdraw_dataset_deletions(conn: Conn, prov: Provenance, source: str, dataset_id: UUID) -> None:
    rows = conn.execute(
        "update harness_decisions set status='expired', decision_note=%s"
        " where status='pending' and action_type='archive_dataset' and payload->>'dataset_id'=%s"
        " and payload->>'source_deleted'=%s returning id",
        (f"withdrawn: back in {source}", str(dataset_id), source)).fetchall()
    for r in rows:
        log_change(conn, prov, "harness", str(r["id"]), "withdraw", after={"reason": f"back in {source}"})


def _propose_dataset_deletion(conn: Conn, actor: Actor, source: str, team_id: UUID, key: str) -> dict | None:
    ds = _dataset_by_key(conn, team_id, f"{source}:{key}")
    if ds is None or ds["archived_at"] is not None:
        return None
    d = harness.propose(conn, actor, "archive_dataset", {"dataset_id": str(ds["id"]), "source_deleted": source},
                        reason=f"deleted in {source}", source=f"connector:{source}")
    return {"key": key, "dataset_id": ds["id"], "decision_id": d["decision_id"], "status": d["status"]}


def apply_batch(conn: Conn, actor: Actor, source: str, team_id: UUID, records: list[Record], cursor: str | None,
                expected_cursor: str | None, deleted: list[str] | None = None,
                dataset_records: list[DatasetRecord] | None = None, deleted_datasets: list[str] | None = None) -> dict:
    """Save `records` and dataset snapshots, propose archiving the deleted keys' items and datasets, and move the
    cursor from `expected_cursor` to `cursor`, in one transaction."""
    _check_source(source)
    actor.require_write(team_id)
    deleted = list(dict.fromkeys(deleted or []))
    dataset_records = list(dataset_records or [])
    deleted_datasets = list(dict.fromkeys(deleted_datasets or []))
    if len(records) + len(deleted) + len(dataset_records) + len(deleted_datasets) > MAX_BATCH:
        raise ValueError(f"at most {MAX_BATCH} records per batch")
    keys = [r.key for r in records]
    ds_keys = [d.key for d in dataset_records]
    if any(not k for k in keys + deleted + ds_keys + deleted_datasets):
        raise ValueError("every record needs a key")
    if set(keys) & set(deleted) or set(ds_keys) & set(deleted_datasets):
        raise ValueError("a key can't be both saved and deleted in one batch")
    if len(set(ds_keys)) != len(ds_keys):
        raise ValueError("a dataset key can appear only once per batch")
    for d in dataset_records:
        datasets.check_snapshot(d.columns, d.rows)
    prov = Provenance(actor.id, f"connector:{source}", f"sync {source}")
    out = {"source": source, "saved": 0, "changed": 0, "erased": 0, "deleted": 0, "items": [], "deletions": [],
           "datasets": [], "datasets_deleted": 0, "access_proposed": 0}
    with transaction(conn, prov):
        conn.execute("insert into sync_cursors (source, team_id) values (%s,%s) on conflict do nothing",
                     (source, team_id))
        row = conn.execute("select cursor from sync_cursors where source=%s and team_id=%s for update",
                           (source, team_id)).fetchone()
        if row["cursor"] != expected_cursor:
            raise CursorConflict(f"cursor for {source} moved (expected {expected_cursor!r}, found {row['cursor']!r})")
        for rec in records:
            ext = f"{source}:{rec.key}"
            readers, dropped = ((None, []) if rec.readers is None and rec.reader_emails is None
                                else _team_readers(conn, team_id, rec.readers, rec.reader_emails))
            exists = readers is not None and conn.execute(
                "select 1 from items where team_id=%s and type=%s and external_key=%s",
                (team_id, rec.type, ext)).fetchone() is not None
            r = store.save_item(conn, actor, prov, team_id=team_id, type=rec.type, title=rec.title, body=rec.body,
                                metadata=rec.metadata, external_key=ext, readers=None if exists else readers)
            out["saved"] += 1
            out["changed"] += bool(r["changed"])
            out["erased"] += bool(r.get("erased"))
            entry = {"key": rec.key, "item_id": r["item_id"], "version": r["version"], "changed": r["changed"]}
            if dropped:
                entry["readers_dropped"] = dropped
            if not r.get("erased"):
                _withdraw_deletions(conn, prov, source, r["item_id"])
                if exists:
                    access = _sync_readers(conn, actor, prov, source, r["item_id"], readers)
                    if access is not None:
                        entry["access_decision"] = access
                        out["access_proposed"] += 1
            out["items"].append(entry)
        for key in deleted:
            d = _propose_deletion(conn, actor, source, team_id, key)
            if d is not None:
                out["deleted"] += 1
                out["deletions"].append(d)
        for d in dataset_records:
            out["datasets"].append(_sync_dataset(conn, actor, prov, source, team_id, d))
        for key in deleted_datasets:
            d = _propose_dataset_deletion(conn, actor, source, team_id, key)
            if d is not None:
                out["datasets_deleted"] += 1
                out["deletions"].append(d)
        conn.execute("update sync_cursors set cursor=%s, batches=batches+1, records=records+%s, updated_at=now(),"
                     " updated_by=%s where source=%s and team_id=%s",
                     (cursor, len(records) + len(dataset_records), actor.id, source, team_id))
        log_change(conn, prov, "sync_cursor", f"{source}/{team_id}", "advance",
                   before={"cursor": expected_cursor},
                   after={"cursor": cursor, "records": len(records), "deleted": len(deleted),
                          **({"datasets": len(dataset_records), "datasets_deleted": len(deleted_datasets)}
                             if dataset_records or deleted_datasets else {})})
    out["cursor"] = cursor
    return out


def run(conn: Conn, actor: Actor, source: str, team_id: UUID, connector: Connector) -> dict:
    """Pull every batch after the stored cursor and apply them in order."""
    cursor = get_cursor(conn, actor, source, team_id)["cursor"]
    if hasattr(connector, "known_datasets"):
        connector.known_datasets = live_dataset_keys(conn, actor, source, team_id)
    total = {"source": source, "batches": 0, "saved": 0, "changed": 0, "erased": 0, "deleted": 0,
             "datasets": 0, "datasets_deleted": 0, "access_proposed": 0, "cursor": cursor}

    def add(r):
        total["batches"] += 1
        for k in ("saved", "changed", "erased", "deleted", "datasets_deleted", "access_proposed"):
            total[k] += r[k]
        total["datasets"] += len(r["datasets"])

    for batch in connector.fetch(cursor):
        add(apply_batch(conn, actor, source, team_id, batch.records, batch.cursor, cursor, batch.deleted,
                        batch.datasets, batch.deleted_datasets))
        cursor = batch.cursor
        total["cursor"] = cursor
    if hasattr(connector, "deleted"):
        gone = connector.deleted(live_keys(conn, actor, source, team_id))
        for i in range(0, len(gone), MAX_BATCH):
            add(apply_batch(conn, actor, source, team_id, [], cursor, cursor, gone[i:i + MAX_BATCH]))
    if hasattr(connector, "deleted_datasets"):
        gone = connector.deleted_datasets(live_dataset_keys(conn, actor, source, team_id))
        for i in range(0, len(gone), MAX_BATCH):
            add(apply_batch(conn, actor, source, team_id, [], cursor, cursor, deleted_datasets=gone[i:i + MAX_BATCH]))
    return total


def live_dataset_keys(conn: Conn, actor: Actor, source: str, team_id: UUID) -> set[str]:
    """Source keys of the team's unarchived datasets from `source`, minus those whose deletion is already pending or
    was rejected (as `live_keys` does for items)."""
    actor.require_write(team_id)
    prefix = f"{_check_source(source)}:"
    rows = conn.execute(
        "select substr(ds.external_key, %s) as key from datasets ds"
        " where ds.team_id=%s and left(ds.external_key, %s)=%s and ds.archived_at is null"
        " and not exists (select 1 from harness_decisions d where d.action_type='archive_dataset'"
        "   and d.payload->>'dataset_id' = ds.id::text and d.payload->>'source_deleted'=%s"
        "   and d.status in ('pending','rejected'))",
        (len(prefix) + 1, team_id, len(prefix), prefix, source)).fetchall()
    return {r["key"] for r in rows}


def live_keys(conn: Conn, actor: Actor, source: str, team_id: UUID) -> set[str]:
    """Source keys of the team's live items from `source`, minus those whose deletion is already pending or was
    rejected (so a full-listing deletion check that finds nothing new writes nothing)."""
    actor.require_write(team_id)
    prefix = f"{_check_source(source)}:"
    rows = conn.execute(
        "select substr(c.external_key, %s) as key from items_current c"
        " where c.team_id=%s and left(c.external_key, %s)=%s and c.status = any(%s)"
        " and not exists (select 1 from harness_decisions d where d.action_type='archive_item'"
        "   and d.payload->>'item_id' = c.id::text and d.payload->>'source_deleted'=%s"
        "   and d.status in ('pending','rejected'))",
        (len(prefix) + 1, team_id, len(prefix), prefix, list(LIVE), source)).fetchall()
    return {r["key"] for r in rows}


# ---------------------------------------------------------------- meeting transcripts (local folder)

TRANSCRIPT_EXT = {".vtt", ".srt", ".txt"}
_TIMING = re.compile(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->\s*(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")
_VOICE = re.compile(r"<v(?:\.[^ >]*)?\s+([^>]+)>(.*?)(?:</v>|$)")
_TAGS = re.compile(r"</?[^>]+>")
_SPEAKER = re.compile(r"^([A-Z][\w.'-]*(?: [A-Z][\w.'-]*){0,3}):\s+(.+)$")


def _seconds(h, m, s, ms) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_transcript(text: str, ext: str) -> tuple[str, dict]:
    """WebVTT / SRT / plain "Speaker: text" → (body as "Speaker: text" paragraphs, metadata)."""
    lines: list[tuple[str | None, str]] = []
    duration = None
    in_note = False
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if ext in {".vtt", ".srt"}:
            if not line:
                in_note = False
                continue
            if in_note or line.startswith("WEBVTT") or line.startswith(("NOTE", "STYLE", "REGION")):
                in_note = True  # header and NOTE/STYLE/REGION blocks run to the next blank line
                continue
            t = _TIMING.match(line)
            if t:
                duration = _seconds(*t.groups()[4:])
                continue
            if line.isdigit():  # cue number
                continue
            v = _VOICE.match(line)
            if v:
                lines.append((v.group(1).strip(), _TAGS.sub("", v.group(2)).strip()))
                continue
            line = _TAGS.sub("", line).strip()
        if not line:
            continue
        s = _SPEAKER.match(line)
        lines.append((s.group(1), s.group(2)) if s else (None, line))
    paras: list[list] = []
    for speaker, said in lines:
        if not said:
            continue
        if paras and paras[-1][0] == speaker:
            paras[-1][1].append(said)
        else:
            paras.append([speaker, [said]])
    body = "\n\n".join((f"{sp}: " if sp else "") + " ".join(words) for sp, words in paras)
    speakers = sorted({sp for sp, _ in paras if sp})
    meta: dict = {"format": ext.lstrip(".")}
    if speakers:
        meta["speakers"] = speakers
    if duration is not None:
        meta["duration_seconds"] = round(duration, 3)
    return body, meta


class TranscriptFolder:
    """Meeting transcripts (.vtt, .srt, .txt) in a folder, e.g. where Zoom/Meet/Teams exports land.

    Cursor: the newest file mtime (ns) already synced. Files at exactly that mtime are re-sent next time, which
    is harmless (identical content adds no version) and means a file written in the same tick isn't missed.
    """

    def __init__(self, root: Path, batch_size: int = 100):
        self.root, self.batch_size = Path(root), batch_size

    def _files(self, since: int) -> list[tuple[int, Path]]:
        found = []
        for p in self.root.rglob("*"):
            rel = p.relative_to(self.root)
            if (p.is_file() and p.suffix.lower() in TRANSCRIPT_EXT
                    and not any(part.startswith(".") for part in rel.parts)):
                m = p.stat().st_mtime_ns
                if m >= since:
                    found.append((m, p))
        return sorted(found, key=lambda x: (x[0], str(x[1])))

    def _check_root(self) -> None:
        if not self.root.is_dir():  # fail closed: an unmounted share must not look like "everything deleted"
            raise FileNotFoundError(f"transcripts folder not found: {self.root}")

    def deleted(self, known: set[str]) -> list[str]:
        """Keys DB4AI holds that are no longer in the folder. An empty folder reports nothing (fail closed)."""
        self._check_root()
        present = {p.relative_to(self.root).as_posix() for _m, p in self._files(0)}
        if not present:
            if known:
                log.warning("transcripts folder %s is empty; not proposing %d deletions", self.root, len(known))
            return []
        return sorted(known - present)

    def fetch(self, cursor: str | None) -> Iterator[Batch]:
        self._check_root()
        files = self._files(int(cursor) if cursor else 0)
        for i in range(0, len(files), self.batch_size):
            chunk = files[i:i + self.batch_size]
            records = []
            for _mtime, p in chunk:
                rel = p.relative_to(self.root).as_posix()
                body, meta = parse_transcript(p.read_text(encoding="utf-8", errors="replace"), p.suffix.lower())
                meta["path"] = rel
                records.append(Record(key=rel, type="meeting", title=p.stem, body=body, metadata=meta))
            yield Batch(records, str(chunk[-1][0]))
