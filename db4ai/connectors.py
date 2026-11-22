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
from typing import Iterator, Protocol
from uuid import UUID

from . import store
from .auth import Actor
from .db import Conn, Provenance, log_change, transaction

MAX_BATCH = 500
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


class Connector(Protocol):
    def fetch(self, cursor: str | None) -> Iterator[Batch]: ...


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


def apply_batch(conn: Conn, actor: Actor, source: str, team_id: UUID, records: list[Record], cursor: str | None,
                expected_cursor: str | None) -> dict:
    """Save `records` and move the cursor from `expected_cursor` to `cursor`, in one transaction."""
    _check_source(source)
    actor.require_write(team_id)
    if len(records) > MAX_BATCH:
        raise ValueError(f"at most {MAX_BATCH} records per batch")
    keys = [r.key for r in records]
    if any(not k for k in keys):
        raise ValueError("every record needs a key")
    prov = Provenance(actor.id, f"connector:{source}", f"sync {source}")
    out = {"source": source, "saved": 0, "changed": 0, "erased": 0, "items": []}
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
        conn.execute("update sync_cursors set cursor=%s, batches=batches+1, records=records+%s, updated_at=now(),"
                     " updated_by=%s where source=%s and team_id=%s",
                     (cursor, len(records), actor.id, source, team_id))
        log_change(conn, prov, "sync_cursor", f"{source}/{team_id}", "advance",
                   before={"cursor": expected_cursor}, after={"cursor": cursor, "records": len(records)})
    out["cursor"] = cursor
    return out


def run(conn: Conn, actor: Actor, source: str, team_id: UUID, connector: Connector) -> dict:
    """Pull every batch after the stored cursor and apply them in order."""
    cursor = get_cursor(conn, actor, source, team_id)["cursor"]
    total = {"source": source, "batches": 0, "saved": 0, "changed": 0, "erased": 0, "cursor": cursor}
    for batch in connector.fetch(cursor):
        r = apply_batch(conn, actor, source, team_id, batch.records, batch.cursor, cursor)
        cursor = batch.cursor
        total["batches"] += 1
        for k in ("saved", "changed", "erased"):
            total[k] += r[k]
        total["cursor"] = cursor
    return total


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

    def fetch(self, cursor: str | None) -> Iterator[Batch]:
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
