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
    # Long items are split into chunks of at most this many characters before embedding.
    chunk_chars: int = field(default_factory=lambda: int(_env("DB4AI_CHUNK_CHARS", "1500")))
    # Vector search first takes this many nearest rows (HNSW with pgvector), then applies team/type filters;
    # if too few results survive it falls back to an exact scan. Capped at 1000 (pgvector's ef_search limit).
    vector_candidates: int = field(default_factory=lambda: int(_env("DB4AI_VECTOR_CANDIDATES", "200")))
    # Notifications for pending harness decisions: comma-separated webhook URLs, optionally prefixed
    # `slack:` (default, {"text": ...}) or `teams:` (Adaptive Card for a Teams Workflows webhook).
    notify_webhooks: str | None = field(default_factory=lambda: _env("DB4AI_NOTIFY_WEBHOOKS"))
    # Wait this long after a proposal before sending, so a burst becomes one message.
    notify_delay_seconds: float = field(default_factory=lambda: float(_env("DB4AI_NOTIFY_DELAY_SECONDS", "60")))
    # Base URL of the web UI, for the review link in notifications.
    public_url: str | None = field(default_factory=lambda: _env("DB4AI_PUBLIC_URL"))
    # Maintainer thresholds
    link_threshold: float = field(default_factory=lambda: float(_env("DB4AI_LINK_THRESHOLD", "0.55")))
    dup_threshold: float = field(default_factory=lambda: float(_env("DB4AI_DUP_THRESHOLD", "0.92")))
    schema_min_items: int = field(default_factory=lambda: int(_env("DB4AI_SCHEMA_MIN_ITEMS", "5")))
    schema_min_share: float = field(default_factory=lambda: float(_env("DB4AI_SCHEMA_MIN_SHARE", "0.8")))


def settings() -> Settings:
    return Settings()
