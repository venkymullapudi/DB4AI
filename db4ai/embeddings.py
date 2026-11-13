"""Pluggable embedders. Default is a local hashing embedder so DB4AI runs fully offline."""

from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Protocol

from .config import settings

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset("a an and are as at be by for from has have in is it its of on or that the this to was were will with we our you".split())


class Embedder(Protocol):
    name: str
    dims: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP and len(t) > 1]


class HashEmbedder:
    """Feature-hashed bag of words + bigrams, L2-normalised. Good enough for dedup and linking."""

    def __init__(self, dims: int = 512):
        self.dims = dims
        self.name = f"hash-{dims}"

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dims
        toks = tokens(text)
        feats = toks + [a + "_" + b for a, b in zip(toks, toks[1:])]
        for f in feats:
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big")
            v[h % self.dims] += 1.0 if (h >> 63) & 1 else -1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


class VoyageEmbedder:
    def __init__(self, model: str):
        import httpx

        self.model, self.name, self.dims = model, f"voyage:{model}", 1024
        self._http = httpx.Client(timeout=60, headers={"Authorization": f"Bearer {os.environ['VOYAGE_API_KEY']}"})

    def embed(self, texts: list[str]) -> list[list[float]]:
        r = self._http.post("https://api.voyageai.com/v1/embeddings",
                            json={"input": [t[:32000] for t in texts], "model": self.model})
        r.raise_for_status()
        return [d["embedding"] for d in r.json()["data"]]


def get_embedder() -> Embedder:
    s = settings()
    if s.embedder == "voyage":
        return VoyageEmbedder(s.voyage_model)
    return HashEmbedder()


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return 0.0 if na == 0 or nb == 0 else dot / (na * nb)
