-- Per-team notification routing (ROADMAP: "Per-team notification routing").
-- A route sends one team's pending harness decisions to the webhook held in an environment variable.
-- Webhook URLs are secrets, so the database stores only the variable's name, never the URL.
-- Changes are recorded in change_log (entity 'notify_route'); removing a route deletes its row.
create table notify_routes (
    team_id     uuid not null references teams(id),
    env_name    text not null check (env_name ~ '^DB4AI_WEBHOOK_[A-Z0-9_]{1,64}$'),
    created_by  uuid not null references actors(id),
    created_at  timestamptz not null default now(),
    primary key (team_id, env_name)
);
