-- `ask` multi-turn context: how many earlier turns the caller supplied (0 = a first question).
-- The earlier questions and answers themselves are never stored.
alter table ask_log add column history_turns smallint not null default 0;
