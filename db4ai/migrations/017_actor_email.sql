-- Optional email per actor, so a connector can map a source's users (e.g. Drive permissions) to actors.
alter table actors add column email text check (email is null or (email = lower(email) and email like '%_@_%'));
create unique index actors_email_key on actors (email) where email is not null;
