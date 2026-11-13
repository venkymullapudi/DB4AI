"""The AI half of the maintainers.

`ClaudeLLM` uses the Anthropic SDK with structured outputs. `HeuristicLLM` needs no network
and is used when no Anthropic credentials are configured (and in tests).
"""

from __future__ import annotations

import logging
import os
import re
from collections import Counter
from typing import Protocol

from pydantic import BaseModel, Field

from .config import settings
from .embeddings import tokens

log = logging.getLogger(__name__)


class Enrichment(BaseModel):
    summary: str = Field(description="One or two sentences.")
    tags: list[str] = Field(description="3-8 short lowercase topic tags.")
    entities: list[str] = Field(description="People, projects, customers, products named in the text.")


class Contradiction(BaseModel):
    contradicts: bool
    explanation: str = Field(description="Which claim in OLD is superseded by NEW, or why not.")
    confidence: float = Field(ge=0, le=1)


class LLM(Protocol):
    name: str

    def enrich(self, title: str, body: str) -> Enrichment: ...
    def contradiction(self, old: str, new: str) -> Contradiction: ...


# ---------------------------------------------------------------- heuristic (offline)

_CAP = re.compile(r"\b([A-Z][a-zA-Z0-9]+(?:\s+[A-Z][a-zA-Z0-9]+)*)\b")
_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2}|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2})\b", re.I)


class HeuristicLLM:
    name = "heuristic"

    def enrich(self, title: str, body: str) -> Enrichment:
        text = f"{title}\n{body}"
        sentences = re.split(r"(?<=[.!?])\s+", body.strip())
        summary = " ".join(sentences[:2])[:400] or title
        counts = Counter(t for t in tokens(text) if not t.isdigit() and len(t) > 3)
        tags = [t for t, _ in counts.most_common(6)]
        starts = {m.start() for m in re.finditer(r"(?:^|[.!?]\s+|\n)", text)}
        ents = []
        for m in _CAP.finditer(text):
            if m.group(1) not in ents and m.start() not in starts and len(m.group(1)) > 2:
                ents.append(m.group(1))
        return Enrichment(summary=summary, tags=tags, entities=ents[:12])

    def contradiction(self, old: str, new: str) -> Contradiction:
        """Same subject, different dates/numbers -> likely superseded."""
        old_t, new_t = set(tokens(old)), set(tokens(new))
        overlap = len(old_t & new_t) / max(1, min(len(old_t), len(new_t)))
        old_facts = {d.lower() for d in _DATE.findall(old)} | set(re.findall(r"\b\d+(?:\.\d+)?\b", old))
        new_facts = {d.lower() for d in _DATE.findall(new)} | set(re.findall(r"\b\d+(?:\.\d+)?\b", new))
        changed = bool(old_facts and new_facts and old_facts != new_facts and not old_facts <= new_facts)
        hit = overlap >= 0.4 and changed
        return Contradiction(
            contradicts=hit,
            explanation=(f"Same topic; facts changed from {sorted(old_facts)} to {sorted(new_facts)}" if hit
                         else "no conflicting facts found"),
            confidence=round(min(0.95, 0.5 + overlap / 2), 2) if hit else 0.0,
        )


# ---------------------------------------------------------------- Claude

class ClaudeLLM:
    def __init__(self, model: str):
        import anthropic

        self.client = anthropic.Anthropic()
        self.model = model
        self.name = f"claude:{model}"
        self._fallback = HeuristicLLM()

    def _parse(self, schema: type[BaseModel], system: str, user: str, fallback):
        import anthropic

        try:
            resp = self.client.messages.parse(
                model=self.model,
                max_tokens=4000,
                output_config={"effort": "low"},
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=schema,
            )
        except (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError) as e:
            raise RuntimeError(f"transient Claude error: {e}") from e  # job retries with backoff
        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            log.warning("Claude declined (%s); using heuristic fallback", resp.stop_reason)
            return fallback()
        return resp.parsed_output

    def enrich(self, title: str, body: str) -> Enrichment:
        return self._parse(
            Enrichment,
            "You maintain a company knowledge database. Extract a faithful summary, topic tags and named "
            "entities from the item. Use only what the text states.",
            f"<title>{title}</title>\n<body>\n{body[:150_000]}\n</body>",
            lambda: self._fallback.enrich(title, body),
        )

    def contradiction(self, old: str, new: str) -> Contradiction:
        return self._parse(
            Contradiction,
            "You maintain a company knowledge database. Decide whether the NEW item supersedes or contradicts "
            "a factual claim in the OLD item (e.g. a changed date, owner, number or decision). Topic overlap "
            "alone is not a contradiction. Set confidence to how sure you are.",
            f"<old>\n{old[:50_000]}\n</old>\n<new>\n{new[:50_000]}\n</new>",
            lambda: self._fallback.contradiction(old, new),
        )


def _has_anthropic_credentials() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    return os.path.isdir(os.path.expanduser("~/.config/anthropic"))


def get_llm() -> LLM:
    s = settings()
    if s.llm == "heuristic" or (s.llm == "auto" and not _has_anthropic_credentials()):
        return HeuristicLLM()
    return ClaudeLLM(s.model)
