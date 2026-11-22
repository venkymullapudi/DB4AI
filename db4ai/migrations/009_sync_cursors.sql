-- Connector framework (ROADMAP: "Connector framework on top of POST /items").
-- One incremental-sync cursor per (source, team). The cursor is opaque to DB4AI (a timestamp, a page token...)
-- and must never hold item content, so erasure has nothing to scrub here. It advances in the same transaction as
-- the items of the batch it covers, with compare-and-set on the previous value; every move is in change_log.
create table sync_cursors (
    source      text not null check (source ~ '^[a-z0-9][a-z0-9_.-]{0,63}$'),
    team_id     uuid not null references teams(id),
    cursor      text,
    batches     bigint not null default 0,
    records     bigint not null default 0,
    updated_at  timestamptz not null default now(),
    updated_by  uuid references actors(id),
    primary key (source, team_id)
);
