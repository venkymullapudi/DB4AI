-- `ask` as of a time: the point in time the answer read items at (null = the current versions).
alter table ask_log add column as_of timestamptz;
