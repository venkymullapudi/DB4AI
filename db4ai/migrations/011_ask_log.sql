-- Audit log of `ask` calls (ROADMAP: "`ask` follow-ups"). Opt-in via DB4AI_ASK_LOG (off | ids | text).
-- Holds ids, versions and a hash of the question; the question text only in `text` mode. Append-only, except that
-- an erasure in progress may rewrite `question` on rows whose sources include the erased/redacted item.
create table ask_log (
    id               uuid primary key default gen_random_uuid(),
    actor_id         uuid not null references actors(id),
    asked_at         timestamptz not null default now(),
    question_sha256  text not null,
    question         text,
    types            text[],
    answered         boolean not null,
    model            text not null,
    sources          jsonb not null default '[]',  -- [{item_id, version}]
    citations        jsonb not null default '[]',  -- [{item_id, version}]
    source_items     uuid[] not null default '{}',
    cited_items      uuid[] not null default '{}'
);
create index ask_log_actor_idx on ask_log (actor_id, asked_at desc);
create index ask_log_source_items_idx on ask_log using gin (source_items);

create function db4ai_ask_log_guard() returns trigger language plpgsql as $$
declare
    target uuid;
begin
    if tg_op = 'UPDATE' then
        target := db4ai_erasure_target();
        if target is not null and target = any(old.source_items)
           and (to_jsonb(new) - 'question') = (to_jsonb(old) - 'question') then
            return new;
        end if;
    end if;
    raise exception 'DB4AI: % is append-only (% blocked)', tg_table_name, tg_op
        using errcode = 'insufficient_privilege';
end $$;

create trigger ask_log_append_only before update or delete on ask_log
    for each row execute function db4ai_ask_log_guard();
