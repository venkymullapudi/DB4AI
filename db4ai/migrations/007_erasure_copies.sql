-- Erasure of copied content (ROADMAP: "Erasure of copied content").
-- During an erasure, change_log rows of data.* tables whose before/after names the target item (item_id
-- column, e.g. a promote_schema backfill) may also lose their content. The check reads the row itself, so
-- it covers rows whose data row was deleted earlier; the scrubbed JSON must still name the same item.

alter table erasures add column data_rows int not null default 0;  -- data.* rows scrubbed in the transaction
alter table erasures add column copies    int not null default 0;  -- redact_item proposals made for copies

create or replace function db4ai_change_log_guard() returns trigger language plpgsql as $$
declare
    target  uuid;
    allowed text[] := array['before', 'after', 'reason'];
begin
    if tg_op = 'UPDATE' then
        target := db4ai_erasure_target();
        if target is not null
           and (to_jsonb(new) - allowed) = (to_jsonb(old) - allowed)
           and (old.entity_id = target::text
                or db4ai_decision_mentions(old.harness_decision_id, target)
                or (old.entity like 'data.%'
                    and target::text in (old.before ->> 'item_id', old.after ->> 'item_id')
                    and new.before ->> 'item_id' is not distinct from old.before ->> 'item_id'
                    and new.after ->> 'item_id' is not distinct from old.after ->> 'item_id')) then
            return new;
        end if;
    end if;
    raise exception 'DB4AI: % is append-only (% blocked)', tg_table_name, tg_op
        using errcode = 'insufficient_privilege';
end $$;
