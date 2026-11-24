-- Per-item ACLs: an item with a row here is readable only by its listed readers (who must also be in the
-- item's team), the team's owners and admins. No row = the whole team, as before. Access narrows, never widens.
create table item_acl (
    item_id      uuid primary key references items(id),
    readers      uuid[] not null,
    set_by       uuid not null references actors(id),
    decision_id  uuid references harness_decisions(id),   -- null when set at creation
    set_at       timestamptz not null default now()
);

-- Team membership is checked by the caller (every read path already filters by team); this adds the ACL.
create function db4ai_item_visible(p_item uuid, p_actor uuid) returns boolean language sql stable as $$
    select p_item is null or not exists (
        select 1 from item_acl a join items i on i.id = a.item_id
        where a.item_id = p_item
          and not (p_actor = any (a.readers))
          and not exists (select 1 from actors x where x.id = p_actor and x.is_admin)
          and not exists (select 1 from team_members m
                          where m.team_id = i.team_id and m.actor_id = p_actor and m.role = 'owner'))
$$;

-- A harness decision is visible only if every item its payload names is.
create function db4ai_decision_visible(p jsonb, p_actor uuid) returns boolean language sql stable as $$
    select db4ai_item_visible((p->>'item_id')::uuid, p_actor)
       and db4ai_item_visible((p->>'src')::uuid, p_actor)
       and db4ai_item_visible((p->>'dst')::uuid, p_actor)
       and db4ai_item_visible((p->>'keep')::uuid, p_actor)
       and db4ai_item_visible((p->>'drop')::uuid, p_actor)
       and db4ai_item_visible((p->>'superseded_by')::uuid, p_actor)
       and db4ai_item_visible((p->>'copy_of')::uuid, p_actor)
$$;

insert into policies (team_id, action_type, tier, min_confidence) values (null, 'set_item_access', 'ask_first', null)
on conflict do nothing;
