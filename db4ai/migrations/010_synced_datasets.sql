-- Datasets synced by connectors (ROADMAP: "Drive Sheets as datasets", part 1: the dataset record kind).
-- A connector sends a full snapshot (header + rows) per source key; the snapshot is diffed into data.<table>.
-- external_key = "<source>:<key>" ties the dataset to its source record. Archiving keeps the table (BI keeps
-- reading it) and only stops syncing; nothing here holds row content.
alter table datasets add column external_key text;
alter table datasets add column archived_at timestamptz;
alter table datasets add column archived_by_decision uuid references harness_decisions(id);
create unique index datasets_external_key_idx on datasets (team_id, external_key) where external_key is not null;

insert into policies (team_id, action_type, tier, min_confidence) values
    (null, 'alter_dataset',   'ask_first', null),
    (null, 'archive_dataset', 'ask_first', null)
on conflict do nothing;
