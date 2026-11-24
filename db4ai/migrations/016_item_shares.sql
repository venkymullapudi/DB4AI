-- Sharing: an item with a row here is also readable by members of the listed teams and by the listed actors
-- (all outside the item's team). Read only; writes stay with the item's team. No row = not shared.
create table item_shares (
    item_id      uuid primary key references items(id),
    teams        uuid[] not null default '{}',
    actors       uuid[] not null default '{}',
    set_by       uuid not null references actors(id),
    decision_id  uuid references harness_decisions(id),
    set_at       timestamptz not null default now()
);

create function db4ai_item_shared(p_item uuid, p_actor uuid) returns boolean language sql stable as $$
    select exists (
        select 1 from item_shares s
        where s.item_id = p_item
          and (p_actor = any (s.actors)
               or exists (select 1 from team_members m where m.actor_id = p_actor and m.team_id = any (s.teams))))
$$;

insert into policies (team_id, action_type, tier, min_confidence) values (null, 'set_item_shares', 'ask_first', null)
on conflict do nothing;
