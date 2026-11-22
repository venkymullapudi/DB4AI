-- Notifications for pending harness decisions (ROADMAP: "Notifications for pending harness decisions").
-- One row per decision per webhook once a message naming it was delivered. Holds ids only (no content),
-- so erasure has nothing to scrub here. `webhook` is a short sha256 of the URL: URLs are secrets and stay in env.
create table notifications (
    decision_id  uuid not null references harness_decisions(id),
    webhook      text not null,
    sent_at      timestamptz not null default now(),
    primary key (decision_id, webhook)
);
