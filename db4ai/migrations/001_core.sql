-- DB4AI core schema. Postgres is the only system of record (SPEC §2).
-- {{EMBEDDING_TYPE}} is replaced by the migration runner with `vector` (pgvector) or `float8[]`.

create extension if not exists pgcrypto;
create schema if not exists data;   -- structured datasets live here, queryable by BI tools

-- ---------------------------------------------------------------- access control (SPEC §8)
create table teams (
    id          uuid primary key default gen_random_uuid(),
    name        text not null unique,
    created_at  timestamptz not null default now()
);

create table actors (
    id          uuid primary key default gen_random_uuid(),
    kind        text not null check (kind in ('human', 'agent')),
    name        text not null unique,
    is_admin    boolean not null default false,
    created_at  timestamptz not null default now()
);

create table team_members (
    team_id   uuid not null references teams(id),
    actor_id  uuid not null references actors(id),
    role      text not null check (role in ('owner', 'member', 'agent')),
    primary key (team_id, actor_id)
);

create table api_tokens (
    token_sha256  text primary key,
    actor_id      uuid not null references actors(id),
    created_at    timestamptz not null default now(),
    revoked_at    timestamptz
);

-- ---------------------------------------------------------------- harness (SPEC §5)
create table harness_decisions (
    id              uuid primary key default gen_random_uuid(),
    action_type     text not null,
    payload         jsonb not null,
    diff            jsonb not null default '{}',
    evidence        jsonb not null default '[]',
    confidence      real,
    tier            text not null check (tier in ('auto', 'auto_notice', 'ask_first')),
    team_id         uuid references teams(id),
    fingerprint     text not null,
    reason          text,
    status          text not null check (status in
                      ('pending', 'approved', 'rejected', 'expired', 'applied_auto', 'reverted')),
    proposed_by     uuid not null references actors(id),
    proposed_at     timestamptz not null default now(),
    expires_at      timestamptz,
    revert_until    timestamptz,
    decided_by      uuid references actors(id),
    decided_at      timestamptz,
    decision_note   text,
    result          jsonb
);
create index harness_pending_idx on harness_decisions (status, team_id) where status = 'pending';
create index harness_fingerprint_idx on harness_decisions (fingerprint);

-- ---------------------------------------------------------------- items (SPEC §3)
create table items (
    id            uuid primary key default gen_random_uuid(),
    team_id       uuid not null references teams(id),
    type          text not null,           -- meeting | document | memory | decision | whiteboard | ...
    external_key  text,                    -- e.g. vault path, for idempotent re-import
    created_at    timestamptz not null default now(),
    created_by    uuid not null references actors(id),
    unique (team_id, type, external_key)
);

create table item_versions (
    id                   bigserial primary key,
    item_id              uuid not null references items(id),
    version              int not null,
    title                text not null default '',
    body                 text not null default '',
    metadata             jsonb not null default '{}',
    status               text not null default 'active' check (status in ('active', 'archived', 'merged', 'stale')),
    merged_into          uuid references items(id),
    blob_sha256          text,
    content_hash         text not null,    -- sha256(title||body||blob): changes only when content changes
    actor_id             uuid not null references actors(id),
    source               text not null,
    reason               text,
    harness_decision_id  uuid references harness_decisions(id),
    created_at           timestamptz not null default clock_timestamp(),
    unique (item_id, version)
);
create index item_versions_fts_idx on item_versions
    using gin (to_tsvector('english', title || ' ' || body));

create view items_current as
select distinct on (v.item_id)
       i.id, i.team_id, i.type, i.external_key, i.created_at, i.created_by,
       v.version, v.title, v.body, v.metadata, v.status, v.merged_into, v.blob_sha256,
       v.content_hash, v.actor_id as updated_by, v.source, v.reason, v.created_at as updated_at
from item_versions v join items i on i.id = v.item_id
order by v.item_id, v.version desc;

-- Derived, rebuildable data (embeddings): upserts allowed.
create table item_embeddings (
    item_id    uuid not null references items(id),
    model      text not null,
    version    int not null,
    embedding  {{EMBEDDING_TYPE}} not null,
    primary key (item_id, model)
);

create table links (
    id                   bigserial primary key,
    src_item             uuid not null references items(id),
    dst_item             uuid not null references items(id),
    kind                 text not null,     -- related | mentions | wikilink | duplicate_of | supersedes
    confidence           real,
    actor_id             uuid not null references actors(id),
    reason               text,
    harness_decision_id  uuid references harness_decisions(id),
    created_at           timestamptz not null default now(),
    unique (src_item, dst_item, kind)
);

create table blobs (
    sha256        text primary key,
    size_bytes    bigint not null,
    content_type  text,
    created_at    timestamptz not null default now()
);

-- ---------------------------------------------------------------- structured datasets
create table datasets (
    id          uuid primary key default gen_random_uuid(),
    team_id     uuid not null references teams(id),
    name        text not null,
    table_name  text not null unique,      -- data.<table_name>
    columns     jsonb not null,
    created_by  uuid not null references actors(id),
    created_at  timestamptz not null default now(),
    harness_decision_id uuid references harness_decisions(id),
    unique (team_id, name)
);

-- ---------------------------------------------------------------- change log (append-only)
create table change_log (
    id                   bigserial primary key,
    at                   timestamptz not null default clock_timestamp(),
    actor_id             uuid,
    source               text,
    reason               text,
    harness_decision_id  uuid,
    entity               text not null,     -- item | link | dataset | data.<table> | harness | policy
    entity_id            text,
    op                   text not null,     -- insert | update | delete | decide | ...
    before               jsonb,
    after                jsonb
);
create index change_log_entity_idx on change_log (entity, entity_id);

-- ---------------------------------------------------------------- policies (risk tiers, SPEC §4)
create table policies (
    id                  bigserial primary key,
    team_id             uuid references teams(id),     -- null = global default
    action_type         text not null,
    tier                text not null check (tier in ('auto', 'auto_notice', 'ask_first')),
    min_confidence      real,     -- below this, escalate to ask_first
    revert_window_days  int not null default 7,
    expires_after_days  int not null default 14
);
create unique index policies_scope_idx on policies (coalesce(team_id, '00000000-0000-0000-0000-000000000000'::uuid), action_type);

insert into policies (team_id, action_type, tier, min_confidence) values
    (null, 'add_tags',              'auto',        null),
    (null, 'set_summary',           'auto',        null),
    (null, 'set_entities',          'auto',        null),
    (null, 'add_link',              'auto',        0.5),
    (null, 'mark_stale',            'auto_notice', 0.7),
    (null, 'merge_items',           'auto_notice', 0.95),
    (null, 'archive_item',          'ask_first',   null),
    (null, 'promote_schema',        'ask_first',   null),
    (null, 'resolve_contradiction', 'ask_first',   null),
    (null, 'change_access',         'ask_first',   null);

-- ---------------------------------------------------------------- job queue / outbox
create table jobs (
    id            bigserial primary key,
    kind          text not null,
    payload       jsonb not null default '{}',
    dedupe_key    text,
    status        text not null default 'queued' check (status in ('queued', 'running', 'done', 'failed')),
    attempts      int not null default 0,
    max_attempts  int not null default 5,
    run_after     timestamptz not null default now(),
    locked_at     timestamptz,
    last_error    text,
    created_at    timestamptz not null default now()
);
create index jobs_ready_idx on jobs (run_after) where status = 'queued';
create unique index jobs_dedupe_idx on jobs (dedupe_key) where status = 'queued';

-- ---------------------------------------------------------------- append-only enforcement
create function db4ai_forbid_mutation() returns trigger language plpgsql as $$
begin
    raise exception 'DB4AI: % is append-only (% blocked)', tg_table_name, tg_op
        using errcode = 'insufficient_privilege';
end $$;

create trigger item_versions_append_only before update or delete on item_versions
    for each row execute function db4ai_forbid_mutation();
create trigger change_log_append_only before update or delete on change_log
    for each row execute function db4ai_forbid_mutation();
create trigger items_append_only before update or delete on items
    for each row execute function db4ai_forbid_mutation();

-- Row-level change capture for structured datasets in schema `data`.
-- Provenance comes from transaction-local settings set by the app (db4ai.actor etc).
create function db4ai_capture_change() returns trigger language plpgsql as $$
begin
    insert into change_log (actor_id, source, reason, harness_decision_id, entity, entity_id, op, before, after)
    values (
        nullif(current_setting('db4ai.actor', true), '')::uuid,
        nullif(current_setting('db4ai.source', true), ''),
        nullif(current_setting('db4ai.reason', true), ''),
        nullif(current_setting('db4ai.decision', true), '')::uuid,
        tg_table_schema || '.' || tg_table_name,
        coalesce((case when tg_op = 'DELETE' then to_jsonb(old) else to_jsonb(new) end) ->> '_row_id', ''),
        lower(tg_op),
        case when tg_op in ('UPDATE', 'DELETE') then to_jsonb(old) end,
        case when tg_op in ('INSERT', 'UPDATE') then to_jsonb(new) end
    );
    return coalesce(new, old);
end $$;

-- Cosine similarity for the float8[] fallback (no pgvector).
create function db4ai_cosine(a float8[], b float8[]) returns float8 language sql immutable as $$
    select case when na = 0 or nb = 0 then 0 else dot / (sqrt(na) * sqrt(nb)) end
    from (select sum(x * y) as dot, sum(x * x) as na, sum(y * y) as nb
          from unnest(a, b) as t(x, y)) s
$$;
