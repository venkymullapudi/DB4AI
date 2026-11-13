"""AIDBHarness: risk-tiered proposals and human decisions (SPEC §4–5).

* The AI never picks a tier — `policies` does, and unknown actions fail closed to `ask_first`.
* Approving applies the change in the same transaction that records the decision.
* Expired decisions are not applied. Rejected/reverted proposals are never re-proposed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from psycopg.types.json import Jsonb

from .actions import REGISTRY
from .auth import AccessDenied, Actor
from .db import Conn, Provenance, log_change, set_provenance


class HarnessError(Exception):
    pass


@dataclass(frozen=True)
class Tier:
    tier: str
    revert_window_days: int
    expires_after_days: int


def resolve_tier(conn: Conn, action_type: str, team_id: UUID | None, confidence: float | None) -> Tier:
    if action_type not in REGISTRY:
        return Tier("ask_first", 0, 14)
    row = conn.execute(
        "select tier, min_confidence, revert_window_days, expires_after_days from policies"
        " where action_type = %s and (team_id = %s or team_id is null)"
        " order by team_id nulls last limit 1",
        (action_type, team_id),
    ).fetchone()
    if row is None:
        return Tier("ask_first", 0, 14)  # fail closed
    tier = row["tier"]
    if tier != "ask_first" and row["min_confidence"] is not None and (confidence is None or confidence < row["min_confidence"]):
        tier = "ask_first"
    return Tier(tier, row["revert_window_days"], row["expires_after_days"])


def set_policy(conn: Conn, actor: Actor, action_type: str, tier: str, team_id: UUID | None = None,
               min_confidence: float | None = None) -> None:
    if not (actor.is_admin or (team_id is not None and actor.can_decide(team_id))):
        raise AccessDenied("only admins or team owners can change policy")
    conn.execute(
        "insert into policies (team_id, action_type, tier, min_confidence) values (%s,%s,%s,%s)"
        " on conflict (coalesce(team_id, '00000000-0000-0000-0000-000000000000'::uuid), action_type)"
        " do update set tier = excluded.tier, min_confidence = excluded.min_confidence",
        (team_id, action_type, tier, min_confidence),
    )
    log_change(conn, Provenance(actor.id, "policy"), "policy", action_type, "upsert",
               after={"team_id": str(team_id) if team_id else None, "tier": tier, "min_confidence": min_confidence})


def fingerprint(action_type: str, payload: dict) -> str:
    stable = {k: v for k, v in payload.items() if k not in ("confidence", "for_version")}
    return hashlib.sha256(json.dumps([action_type, stable], sort_keys=True, default=str).encode()).hexdigest()


def _apply(conn: Conn, decision: dict, prov: Provenance, payload: dict) -> dict:
    action = REGISTRY[decision["action_type"]]
    set_provenance(conn, prov)  # row triggers on data.* tables attribute changes to this decision
    result = action.apply(conn, prov, payload) or {}
    if "versions" in result:  # remember post-action versions so a later revert can detect conflicts
        result["after_versions"] = {
            iid: conn.execute("select max(version) as v from item_versions where item_id=%s", (iid,)).fetchone()["v"]
            for iid in result["versions"]}
    return result


def propose(conn: Conn, agent: Actor, action_type: str, payload: dict, *, reason: str,
            evidence: list | None = None, confidence: float | None = None, source: str = "maintainer") -> dict:
    """Propose an action. Auto tiers apply immediately; ask_first is queued for a human.

    Must be called inside a transaction.
    """
    fp = fingerprint(action_type, payload)
    prior = conn.execute(
        "select id, status from harness_decisions where fingerprint = %s"
        " and status in ('pending','approved','applied_auto','rejected','reverted') limit 1", (fp,)).fetchone()
    if prior is not None:
        return {"decision_id": prior["id"], "status": prior["status"], "skipped": True}

    action = REGISTRY.get(action_type)
    team_id = action.team_of(conn, payload) if action else None
    tier = resolve_tier(conn, action_type, team_id, confidence)
    diff = action.diff(conn, payload) if action else {"unknown_action": action_type, "payload": payload}
    auto = tier.tier in ("auto", "auto_notice") and action is not None
    decision = conn.execute(
        "insert into harness_decisions (action_type, payload, diff, evidence, confidence, tier, team_id, fingerprint,"
        " reason, status, proposed_by, expires_at, revert_until)"
        " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
        "  case when %s then null else now() + make_interval(days => %s) end,"
        "  case when %s = 'auto_notice' then now() + make_interval(days => %s) end)"
        " returning *",
        (action_type, Jsonb(payload), Jsonb(diff), Jsonb(evidence or []), confidence, tier.tier, team_id, fp,
         reason, "applied_auto" if auto else "pending", agent.id,
         auto, tier.expires_after_days, tier.tier, tier.revert_window_days),
    ).fetchone()
    prov = Provenance(agent.id, source, reason, decision["id"])
    if auto:
        result = _apply(conn, decision, prov, payload)
        conn.execute("update harness_decisions set result=%s where id=%s", (Jsonb(result), decision["id"]))
    log_change(conn, prov, "harness", str(decision["id"]), "propose",
               after={"action": action_type, "tier": tier.tier, "status": decision["status"]})
    return {"decision_id": decision["id"], "status": decision["status"], "tier": tier.tier, "skipped": False}


def _lock(conn: Conn, decision_id: UUID) -> dict:
    d = conn.execute("select * from harness_decisions where id=%s for update", (decision_id,)).fetchone()
    if d is None:
        raise HarnessError(f"no decision {decision_id}")
    return d


def decide(conn: Conn, actor: Actor, decision_id: UUID, approve: bool, note: str | None = None,
           edited_payload: dict | None = None) -> dict:
    """Approve (applies the change atomically with the decision) or reject a pending decision."""
    with conn.transaction():
        d = _lock(conn, decision_id)
        if not actor.can_decide(d["team_id"]):
            raise AccessDenied(f"{actor.name} cannot decide for this scope")
        if d["status"] != "pending":
            raise HarnessError(f"decision is {d['status']}, not pending")
        expired = conn.execute("select %s::timestamptz < now() as x", (d["expires_at"],)).fetchone()["x"]
        if d["expires_at"] is not None and expired:
            conn.execute("update harness_decisions set status='expired' where id=%s", (decision_id,))
            log_change(conn, Provenance(actor.id, "harness"), "harness", str(decision_id), "expire")
            return {"decision_id": decision_id, "status": "expired"}
        if d["action_type"] not in REGISTRY:
            approve = False
            note = (note or "") + " [unknown action type: cannot be applied]"
        prov = Provenance(actor.id, "harness", note or d["reason"], decision_id)
        result = None
        if approve:
            payload = edited_payload if edited_payload is not None else d["payload"]
            result = _apply(conn, d, prov, payload)
        status = "approved" if approve else "rejected"
        conn.execute(
            "update harness_decisions set status=%s, decided_by=%s, decided_at=now(), decision_note=%s,"
            " result=%s, payload = coalesce(%s, payload) where id=%s",
            (status, actor.id, note, Jsonb(result) if result is not None else None,
             Jsonb(edited_payload) if edited_payload is not None else None, decision_id),
        )
        log_change(conn, prov, "harness", str(decision_id), "decide",
                   before={"status": "pending"}, after={"status": status, "edited": edited_payload is not None})
    return {"decision_id": decision_id, "status": status, "result": result}


def revert(conn: Conn, actor: Actor, decision_id: UUID, note: str | None = None) -> dict:
    """Undo an applied decision (auto_notice within its window, or any by an admin)."""
    with conn.transaction():
        d = _lock(conn, decision_id)
        if d["status"] not in ("applied_auto", "approved"):
            raise HarnessError(f"decision is {d['status']}; nothing to revert")
        if actor.kind == "agent" or not (actor.is_admin or actor.can_read(d["team_id"])):
            raise AccessDenied("not allowed to revert this decision")
        in_window = d["revert_until"] is not None and conn.execute(
            "select %s::timestamptz > now() as x", (d["revert_until"],)).fetchone()["x"]
        if not (in_window or actor.is_admin):
            raise AccessDenied("revert window closed; ask an admin")
        action = REGISTRY.get(d["action_type"])
        if action is None or action.revert is None:
            raise HarnessError(f"{d['action_type']} cannot be reverted automatically")
        result = d["result"] or {}
        for iid, after in result.get("after_versions", {}).items():
            cur = conn.execute("select max(version) as v from item_versions where item_id=%s", (iid,)).fetchone()["v"]
            if cur != after:
                raise HarnessError(f"item {iid} changed after this decision (v{after} -> v{cur}); revert it manually")
        prov = Provenance(actor.id, "harness", note or f"revert {d['action_type']}", decision_id)
        set_provenance(conn, prov)
        action.revert(conn, prov, result)
        conn.execute("update harness_decisions set status='reverted', decision_note=%s where id=%s", (note, decision_id))
        log_change(conn, prov, "harness", str(decision_id), "revert", before={"status": d["status"]}, after={"status": "reverted"})
    return {"decision_id": decision_id, "status": "reverted"}


def expire_pending(conn: Conn) -> int:
    rows = conn.execute(
        "update harness_decisions set status='expired' where status='pending' and expires_at < now() returning id"
    ).fetchall()
    for r in rows:
        conn.execute("insert into change_log (entity, entity_id, op, source) values ('harness', %s, 'expire', 'hygiene')",
                     (str(r["id"]),))
    return len(rows)


def list_decisions(conn: Conn, actor: Actor, status: str = "pending", limit: int = 50) -> list[dict]:
    teams = None if actor.is_admin else list(actor.teams)
    return conn.execute(
        "select d.id, d.action_type, d.tier, d.status, d.confidence, d.reason, d.diff, d.evidence, d.payload,"
        " d.proposed_at, d.expires_at, d.revert_until, t.name as team, a.name as proposed_by"
        " from harness_decisions d left join teams t on t.id = d.team_id join actors a on a.id = d.proposed_by"
        " where d.status = %s and (%s::uuid[] is null or d.team_id = any(%s::uuid[]))"
        " order by d.proposed_at desc limit %s",
        (status, teams, teams, limit),
    ).fetchall()
