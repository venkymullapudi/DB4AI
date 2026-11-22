"""Connector framework: incremental sync from external sources (SPEC §6).

A connector turns a source (a folder of transcripts, later Google Drive...) into batches of `Record`s plus the
cursor reached after each batch. `apply_batch` saves a batch the same way `POST /items` saves one item and moves
the source's cursor in the same transaction, with compare-and-set on the previous cursor, so a crash never skips
records and two syncers of one source can't interleave. Re-sending a record is harmless: identical content adds
no version. HTTP connectors that run elsewhere use the same function through `POST /sync/{source}`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
import logging
from typing import Iterator, Protocol
from uuid import UUID

from . import harness, store
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


@dataclass
class Batch:
    records: list[Record]
    cursor: str | None       # where the source is once these records are saved
    deleted: list[str] = field(default_factory=list)  # keys the source deleted → propose archive_item


class Connector(Protocol):
    def fetch(self, cursor: str | None) -> Iterator[Batch]: ...

    # Optional, for sources whose cursor can't see deletions (e.g. a folder): given the keys DB4AI holds as live,
    # return those the source no longer has. Called by `run` after the batches.
    # def deleted(self, known: set[str]) -> list[str]: ...


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


def apply_batch(conn: Conn, actor: Actor, source: str, team_id: UUID, records: list[Record], cursor: str | None,
                expected_cursor: str | None, deleted: list[str] | None = None) -> dict:
    """Save `records`, propose archiving the `deleted` keys' items, and move the cursor from `expected_cursor` to
    `cursor`, in one transaction."""
    _check_source(source)
    actor.require_write(team_id)
    deleted = list(dict.fromkeys(deleted or []))
    if len(records) + len(deleted) > MAX_BATCH:
        raise ValueError(f"at most {MAX_BATCH} records per batch")
    keys = [r.key for r in records]
    if any(not k for k in keys + deleted):
        raise ValueError("every record needs a key")
    if set(keys) & set(deleted):
        raise ValueError("a key can't be both saved and deleted in one batch")
    prov = Provenance(actor.id, f"connector:{source}", f"sync {source}")
    out = {"source": source, "saved": 0, "changed": 0, "erased": 0, "deleted": 0, "items": [], "deletions": []}
    with transaction(conn, prov):
        conn.execute("insert into sync_cursors (source, team_id) values (%s,%s) on conflict do nothing",
                     (source, team_id))
        row = conn.execute("select cursor from sync_cursors where source=%s and team_id=%s for update",
                           (source, team_id)).fetchone()
        if row["cursor"] != expected_cursor:
            raise CursorConflict(f"cursor for {source} moved (expected {expected_cursor!r}, found {row['cursor']!r})")
        for rec in records:
            r = store.save_item(conn, actor, prov, team_id=team_id, type=rec.type, title=rec.title, body=rec.body,
                                metadata=rec.metadata, external_key=f"{source}:{rec.key}")
            out["saved"] += 1
            out["changed"] += bool(r["changed"])
            out["erased"] += bool(r.get("erased"))
            out["items"].append({"key": rec.key, "item_id": r["item_id"], "version": r["version"],
                                 "changed": r["changed"]})
            if not r.get("erased"):
                _withdraw_deletions(conn, prov, source, r["item_id"])
        for key in deleted:
            d = _propose_deletion(conn, actor, source, team_id, key)
            if d is not None:
                out["deleted"] += 1
                out["deletions"].append(d)
        conn.execute("update sync_cursors set cursor=%s, batches=batches+1, records=records+%s, updated_at=now(),"
                     " updated_by=%s where source=%s and team_id=%s",
                     (cursor, len(records), actor.id, source, team_id))
        log_change(conn, prov, "sync_cursor", f"{source}/{team_id}", "advance",
                   before={"cursor": expected_cursor},
                   after={"cursor": cursor, "records": len(records), "deleted": len(deleted)})
    out["cursor"] = cursor
    return out


def run(conn: Conn, actor: Actor, source: str, team_id: UUID, connector: Connector) -> dict:
    """Pull every batch after the stored cursor and apply them in order."""
    cursor = get_cursor(conn, actor, source, team_id)["cursor"]
    total = {"source": source, "batches": 0, "saved": 0, "changed": 0, "erased": 0, "deleted": 0, "cursor": cursor}

    def add(r):
        total["batches"] += 1
        for k in ("saved", "changed", "erased", "deleted"):
            total[k] += r[k]

    for batch in connector.fetch(cursor):
        add(apply_batch(conn, actor, source, team_id, batch.records, batch.cursor, cursor, batch.deleted))
        cursor = batch.cursor
        total["cursor"] = cursor
    if hasattr(connector, "deleted"):
        gone = connector.deleted(live_keys(conn, actor, source, team_id))
        for i in range(0, len(gone), MAX_BATCH):
            add(apply_batch(conn, actor, source, team_id, [], cursor, cursor, gone[i:i + MAX_BATCH]))
    return total


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
