-- Current-version pointer (ROADMAP: "Current-version pointer").
-- item_heads is derived data: one row per item pointing at its latest version. It is maintained by a
-- trigger in the same transaction as each version insert, so it can never disagree with item_versions
-- after commit, whichever code path wrote the version.

create table item_heads (
    item_id     uuid primary key references items(id),
    version_id  bigint not null references item_versions(id),
    version     int not null
);

create function db4ai_advance_head() returns trigger language plpgsql as $$
begin
    insert into item_heads (item_id, version_id, version) values (new.item_id, new.id, new.version)
    on conflict (item_id) do update set version_id = excluded.version_id, version = excluded.version
        where item_heads.version < excluded.version;
    return new;
end $$;

create trigger item_versions_advance_head after insert on item_versions
    for each row execute function db4ai_advance_head();

-- Heads only move forward and only via the trigger above.
create function db4ai_guard_head() returns trigger language plpgsql as $$
begin
    if tg_op = 'DELETE' then
        raise exception 'DB4AI: item_heads rows cannot be deleted' using errcode = 'insufficient_privilege';
    end if;
    if new.item_id <> old.item_id or new.version <= old.version
       or not exists (select 1 from item_versions v
                      where v.id = new.version_id and v.item_id = new.item_id and v.version = new.version) then
        raise exception 'DB4AI: item_heads may only advance to an existing later version'
            using errcode = 'insufficient_privilege';
    end if;
    return new;
end $$;

create trigger item_heads_guard before update or delete on item_heads
    for each row execute function db4ai_guard_head();

-- Backfill existing items.
insert into item_heads (item_id, version_id, version)
select distinct on (item_id) item_id, id, version from item_versions order by item_id, version desc;

create or replace view items_current as
select i.id, i.team_id, i.type, i.external_key, i.created_at, i.created_by,
       v.version, v.title, v.body, v.metadata, v.status, v.merged_into, v.blob_sha256,
       v.content_hash, v.actor_id as updated_by, v.source, v.reason, v.created_at as updated_at
from item_heads h
join items i on i.id = h.item_id
join item_versions v on v.id = h.version_id;
