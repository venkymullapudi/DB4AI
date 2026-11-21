-- Chunked embeddings (ROADMAP: "Chunked embeddings + vector index", part 1).
-- Derived, rebuildable data: one row per (item, model, chunk). Chunk text is not stored; start_char/end_char
-- are offsets into the body of the version whose content_hash is recorded. Search ignores chunks whose
-- content_hash no longer matches the item's current version.
-- {{EMBEDDING_TYPE}} is replaced by the migration runner with `vector` (pgvector) or `float8[]`.

create table item_chunks (
    item_id       uuid not null references items(id),
    model         text not null,
    chunk_no      int not null,
    version       int not null,
    content_hash  text not null,
    start_char    int not null,
    end_char      int not null,
    embedding     {{EMBEDDING_TYPE}} not null,
    primary key (item_id, model, chunk_no),
    check (0 <= start_char and start_char <= end_char)
);
