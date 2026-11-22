-- One version per enrichment: summary, tags and entities are applied by a single `enrich` action.
-- Global default is `auto`, like the three per-field actions it replaces for the maintainer.
insert into policies (team_id, action_type, tier, min_confidence)
values (null, 'enrich', 'auto', null)
on conflict do nothing;

-- Teams that tightened any per-field action keep that strictness for `enrich` (fail closed).
insert into policies (team_id, action_type, tier, min_confidence, revert_window_days, expires_after_days)
select team_id, 'enrich',
       (array['auto', 'auto_notice', 'ask_first'])[max(case tier when 'auto' then 1 when 'auto_notice' then 2 else 3 end)],
       max(min_confidence), max(revert_window_days), min(expires_after_days)
from policies
where team_id is not null and action_type in ('add_tags', 'set_summary', 'set_entities')
group by team_id
on conflict do nothing;
