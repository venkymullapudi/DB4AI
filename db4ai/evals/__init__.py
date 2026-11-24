"""Evaluation set for the dedup and staleness maintainers.

Each case is a labelled pair (`old` = the existing item, `new` = the item being related). The eval runs the same
pair rules and LLM call as `maintainers.relate`, in memory: it never touches a database or proposes anything.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from ..embeddings import Embedder, cosine
from ..maintainers import STALE_MIN_SIMILARITY, duplicate_pair, staleness_pair

TASKS = ("dedup", "staleness")


def load_cases(path: Path | None = None) -> list[dict]:
    """The built-in set, or an owner's own file in the same format. Raises ValueError if malformed."""
    if path is None:
        text = resources.files(__package__).joinpath("maintainers.json").read_text()
    else:
        text = Path(path).read_text()
    data = json.loads(text)
    cases = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(cases, list) or not cases:
        raise ValueError("eval file needs a non-empty 'cases' list")
    seen: set[str] = set()
    for c in cases:
        cid = c.get("id")
        if not isinstance(cid, str) or not cid or cid in seen:
            raise ValueError(f"case id missing or repeated: {cid!r}")
        seen.add(cid)
        if c.get("task") not in TASKS:
            raise ValueError(f"{cid}: task must be one of {TASKS}")
        if not isinstance(c.get("expected"), bool):
            raise ValueError(f"{cid}: expected must be true or false")
        for side in ("old", "new"):
            it = c.get(side)
            if not isinstance(it, dict) or not all(isinstance(it.get(k), str) for k in ("type", "title", "body")):
                raise ValueError(f"{cid}: {side} needs type, title and body strings")
    return cases


def _text(item: dict) -> str:
    return f"{item['title']}\n{item['body']}"  # as `enrich` embeds it


def _metrics(rows: list[tuple[bool, bool]]) -> dict:
    tp = sum(e and p for e, p in rows)
    fp = sum(p and not e for e, p in rows)
    fn = sum(e and not p for e, p in rows)
    tn = len(rows) - tp - fp - fn
    precision = tp / (tp + fp) if tp + fp else 1.0  # no proposals made: nothing wrong was proposed
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"cases": len(rows), "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": round(precision, 3), "recall": round(recall, 3), "f1": round(f1, 3)}


def run(cases: list[dict], llm, embedder: Embedder, dup_threshold: float) -> dict:
    """Score each case as `relate` would judge the pair; returns metrics per task and every miss."""
    texts = [t for c in cases for t in (_text(c["old"]), _text(c["new"]))]
    vecs = embedder.embed(texts)
    rows: dict[str, list[tuple[bool, bool]]] = {t: [] for t in TASKS}
    misses = []
    for n, c in enumerate(cases):
        old, new = c["old"], c["new"]
        score = cosine(vecs[2 * n], vecs[2 * n + 1])
        explanation = None
        dup = duplicate_pair(score, new["type"], old["type"], dup_threshold)
        if c["task"] == "dedup":
            predicted = dup
        else:  # relate never checks staleness for a pair it proposes to merge
            predicted = False
            if not dup and staleness_pair(score, new["type"], old["type"], c.get("old_is_older", True)):
                verdict = llm.contradiction(_text(old), _text(new))
                predicted, explanation = verdict.contradicts, verdict.explanation
        rows[c["task"]].append((c["expected"], predicted))
        if predicted != c["expected"]:
            misses.append({"id": c["id"], "task": c["task"], "expected": c["expected"], "predicted": predicted,
                           "similarity": round(score, 3), "explanation": explanation, "note": c.get("note")})
    return {
        "llm": getattr(llm, "name", type(llm).__name__),
        "embedder": getattr(embedder, "name", type(embedder).__name__),
        "thresholds": {"dup": dup_threshold, "stale_min_similarity": STALE_MIN_SIMILARITY},
        "tasks": {t: _metrics(r) for t, r in rows.items() if r},
        "misses": misses,
    }
