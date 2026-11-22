-- Redaction and erasure via the AIDBHarness (ROADMAP: "Redaction and erasure").
-- The only sanctioned bypass of the append-only triggers: while a pending `erase_item` / `redact_item`
-- decision is being applied (approve = apply, one transaction), the app sets `db4ai.erasure` to that
-- decision's id, and rows of *that decision's item* may have their content columns rewritten.
-- Deletes stay forbidden, and identity/provenance columns (ids, version numbers, actor, source,
-- decision, timestamps) can never change.

alter table item_versions drop constraint item_versions_status_check;
alter table item_versions add constraint item_versions_status_check
    check (status in ('active', 'archived', 'merged', 'stale', 'erased'));

-- Tombstones: what was erased or redacted, by whom, and why. Never holds the erased content.
create table erasures (
    id              uuid primary key default gen_random_uuid(),
    item_id         uuid not null references items(id),
    decision_id     uuid not null unique references harness_decisions(id),
    kind            text not null check (kind in ('erase', 'redact')),
    requested_by    uuid not null references actors(id),
    approved_by     uuid not null references actors(id),
    reason          text not null,
    versions        int not null,          -- versions scrubbed
    terms           int not null default 0, -- redaction terms (count only; the terms themselves are scrubbed)
    matches         int not null default 0, -- occurrences replaced
    blobs_deleted   int not null default 0,
    blobs_kept      int not null default 0, -- blobs also referenced by other items, so not deleted
    created_at      timestamptz not null default now()
);
create index erasures_item_idx on erasures (item_id);
create trigger erasures_append_only before update or delete on erasures
    for each row execute function db4ai_forbid_mutation();

-- The item the erasure decision in progress may rewrite, or null.
create function db4ai_erasure_target() returns uuid language sql stable as $$
    select (d.payload ->> 'item_id')::uuid
    from harness_decisions d
    where d.id = nullif(current_setting('db4ai.erasure', true), '')::uuid
      and d.action_type in ('erase_item', 'redact_item')
      and d.status = 'pending'
$$;

-- Append-only, except that during an erasure the target item's rows may change the listed columns.
-- tg_argv[0] = the column holding the item id; tg_argv[1..] = columns that may be rewritten.
create function db4ai_append_only_except_erasure() returns trigger language plpgsql as $$
declare
    target  uuid;
    id_col  text := tg_argv[0];
    allowed text[] := tg_argv[1:array_upper(tg_argv, 1)];
begin
    if tg_op = 'UPDATE' then
        target := db4ai_erasure_target();
        if target is not null
           and to_jsonb(old) ->> id_col = target::text
           and to_jsonb(new) ->> id_col = target::text
           and (to_jsonb(new) - allowed) = (to_jsonb(old) - allowed) then
            return new;
        end if;
    end if;
    raise exception 'DB4AI: % is append-only (% blocked)', tg_table_name, tg_op
        using errcode = 'insufficient_privilege';
end $$;

drop trigger item_versions_append_only on item_versions;
create trigger item_versions_append_only before update or delete on item_versions
    for each row execute function db4ai_append_only_except_erasure(
        'item_id', 'title', 'body', 'metadata', 'blob_sha256', 'content_hash', 'reason');

drop trigger items_append_only on items;
create trigger items_append_only before update or delete on items
    for each row execute function db4ai_append_only_except_erasure('id', 'external_key');

-- change_log rows about the item (entity_id = item id), or written under a harness decision that mentions
-- the item, may lose their content (before/after/reason). Nothing else about them can change.
create function db4ai_decision_mentions(decision uuid, item uuid) returns boolean language sql stable as $$
    select exists (select 1 from harness_decisions d where d.id = decision and item::text in (
        d.payload ->> 'item_id', d.payload ->> 'src', d.payload ->> 'dst', d.payload ->> 'keep',
        d.payload ->> 'drop', d.payload ->> 'superseded_by'))
$$;

create function db4ai_change_log_guard() returns trigger language plpgsql as $$
declare
    target  uuid;
    allowed text[] := array['before', 'after', 'reason'];
begin
    if tg_op = 'UPDATE' then
        target := db4ai_erasure_target();
        if target is not null
           and (to_jsonb(new) - allowed) = (to_jsonb(old) - allowed)
           and (old.entity_id = target::text or db4ai_decision_mentions(old.harness_decision_id, target)) then
            return new;
        end if;
    end if;
    raise exception 'DB4AI: % is append-only (% blocked)', tg_table_name, tg_op
        using errcode = 'insufficient_privilege';
end $$;

drop trigger change_log_append_only on change_log;
create trigger change_log_append_only before update or delete on change_log
    for each row execute function db4ai_change_log_guard();

-- Always ask_first and admin-only (also enforced in code, so a policy row can't loosen it).
insert into policies (team_id, action_type, tier, min_confidence, expires_after_days) values
    (null, 'erase_item',  'ask_first', null, 30),
    (null, 'redact_item', 'ask_first', null, 30)
on conflict do nothing;
