-- Vector index (ROADMAP: "Vector index").
-- Each embedding model has one fixed dimension count, recorded in `embedding_models`. With pgvector, every model
-- gets a partial HNSW index per embeddings table on `(embedding::vector(dims))`, so a single untyped `vector`
-- column can hold several models and each one is still indexable. Without pgvector (`float8[]` fallback) the
-- registry still enforces fixed dims, and search does an exact scan.

create table embedding_models (
    model       text primary key,
    dims        int not null check (dims > 0),
    created_at  timestamptz not null default now()
);

create function db4ai_embedding_is_vector() returns boolean language sql stable as $$
    select coalesce((select format_type(atttypid, atttypmod) like 'vector%' from pg_attribute
                     where attrelid = 'item_embeddings'::regclass and attname = 'embedding'), false)
$$;

-- Create the per-model HNSW indexes if they don't exist yet. No-op without pgvector, and above pgvector's
-- 2000-dimension HNSW limit (those models fall back to an exact scan).
create function db4ai_ensure_vector_index(p_model text, p_dims int) returns void language plpgsql as $$
declare
    tag text := substr(md5(p_model), 1, 12);
    tbl text;
begin
    if not db4ai_embedding_is_vector() or p_dims > 2000 then
        return;
    end if;
    foreach tbl in array array['item_embeddings', 'item_chunks'] loop
        execute format('create index if not exists %I on %I using hnsw ((embedding::vector(%s)) vector_cosine_ops)'
                       ' where model = %L', tbl || '_hnsw_' || tag, tbl, p_dims, p_model);
    end loop;
end $$;

-- Backfill: register each existing model at its most common dimension count, drop derived rows of any other
-- size (they can't be indexed; hygiene re-queues those items for enrichment), then build the indexes.
do $$
declare
    dims_expr text := case when db4ai_embedding_is_vector() then 'vector_dims(embedding)'
                           else 'array_length(embedding, 1)' end;
    m record;
begin
    execute format(
        'insert into embedding_models (model, dims)'
        ' select distinct on (model) model, d from ('
        '   select model, %1$s as d from item_embeddings union all select model, %1$s from item_chunks) x'
        ' where d is not null group by model, d order by model, count(*) desc, d', dims_expr);
    execute format('delete from item_embeddings e using embedding_models m'
                   ' where m.model = e.model and coalesce(%s, 0) <> m.dims', replace(dims_expr, 'embedding', 'e.embedding'));
    execute format('delete from item_chunks k using embedding_models m'
                   ' where m.model = k.model and coalesce(%s, 0) <> m.dims', replace(dims_expr, 'embedding', 'k.embedding'));
    for m in select model, dims from embedding_models loop
        perform db4ai_ensure_vector_index(m.model, m.dims);
    end loop;
end $$;
