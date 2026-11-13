"""Runtime settings, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: _env("DB4AI_DATABASE_URL", "postgresql://localhost/db4ai"))
    # Blob store: "file:///path" or "s3://bucket/prefix"
    blob_url: str = field(default_factory=lambda: _env("DB4AI_BLOB_URL", "file://./.db4ai-blobs"))
    # Claude model for maintainers; unset ANTHROPIC credentials -> heuristic fallback.
    model: str = field(default_factory=lambda: _env("DB4AI_MODEL", "claude-opus-5-5"))
    llm: str = field(default_factory=lambda: _env("DB4AI_LLM", "auto"))  # auto | claude | heuristic
    # Embeddings: hash (local, offline) | voyage
    embedder: str = field(default_factory=lambda: _env("DB4AI_EMBEDDER", "hash"))
    voyage_model: str = field(default_factory=lambda: _env("DB4AI_VOYAGE_MODEL", "voyage-3.5"))
    # Maintainer thresholds
    link_threshold: float = field(default_factory=lambda: float(_env("DB4AI_LINK_THRESHOLD", "0.55")))
    dup_threshold: float = field(default_factory=lambda: float(_env("DB4AI_DUP_THRESHOLD", "0.92")))
    schema_min_items: int = field(default_factory=lambda: int(_env("DB4AI_SCHEMA_MIN_ITEMS", "5")))
    schema_min_share: float = field(default_factory=lambda: float(_env("DB4AI_SCHEMA_MIN_SHARE", "0.8")))


def settings() -> Settings:
    return Settings()
