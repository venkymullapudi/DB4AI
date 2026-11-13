"""Teams, actors, roles and API tokens (SPEC §8)."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from uuid import UUID

from .db import Conn


class AccessDenied(Exception):
    pass


@dataclass(frozen=True)
class Actor:
    id: UUID
    name: str
    kind: str  # human | agent
    is_admin: bool
    teams: dict[UUID, str]  # team_id -> role

    def can_read(self, team_id: UUID) -> bool:
        return self.is_admin or team_id in self.teams

    def can_write(self, team_id: UUID) -> bool:
        return self.can_read(team_id)

    def can_decide(self, team_id: UUID | None) -> bool:
        """Harness decisions: admins anywhere, owners within their team. Agents never."""
        if self.kind == "agent":
            return False
        if self.is_admin:
            return True
        return team_id is not None and self.teams.get(team_id) == "owner"

    def require_read(self, team_id: UUID) -> None:
        if not self.can_read(team_id):
            raise AccessDenied(f"{self.name} cannot access team {team_id}")

    def require_write(self, team_id: UUID) -> None:
        if not self.can_write(team_id):
            raise AccessDenied(f"{self.name} cannot write to team {team_id}")


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_team(conn: Conn, name: str) -> UUID:
    row = conn.execute(
        "insert into teams (name) values (%s) on conflict (name) do update set name = excluded.name returning id",
        (name,),
    ).fetchone()
    return row["id"]


def team_id_by_name(conn: Conn, name: str) -> UUID:
    row = conn.execute("select id from teams where name = %s", (name,)).fetchone()
    if row is None:
        raise KeyError(f"no team named {name!r}")
    return row["id"]


def create_actor(conn: Conn, name: str, kind: str = "human", is_admin: bool = False) -> UUID:
    row = conn.execute(
        "insert into actors (name, kind, is_admin) values (%s, %s, %s)"
        " on conflict (name) do update set is_admin = excluded.is_admin returning id",
        (name, kind, is_admin),
    ).fetchone()
    return row["id"]


def add_member(conn: Conn, team_id: UUID, actor_id: UUID, role: str = "member") -> None:
    conn.execute(
        "insert into team_members (team_id, actor_id, role) values (%s, %s, %s)"
        " on conflict (team_id, actor_id) do update set role = excluded.role",
        (team_id, actor_id, role),
    )


def issue_token(conn: Conn, actor_id: UUID) -> str:
    token = "db4ai_" + secrets.token_urlsafe(32)
    conn.execute("insert into api_tokens (token_sha256, actor_id) values (%s, %s)", (_sha(token), actor_id))
    return token


def load_actor(conn: Conn, actor_id: UUID) -> Actor:
    row = conn.execute("select id, name, kind, is_admin from actors where id = %s", (actor_id,)).fetchone()
    if row is None:
        raise AccessDenied("unknown actor")
    teams = {r["team_id"]: r["role"] for r in conn.execute(
        "select team_id, role from team_members where actor_id = %s", (actor_id,))}
    return Actor(row["id"], row["name"], row["kind"], row["is_admin"], teams)


def actor_for_token(conn: Conn, token: str) -> Actor:
    row = conn.execute(
        "select actor_id from api_tokens where token_sha256 = %s and revoked_at is null", (_sha(token),)
    ).fetchone()
    if row is None:
        raise AccessDenied("invalid or revoked token")
    return load_actor(conn, row["actor_id"])


def maintainer_agent(conn: Conn) -> Actor:
    """The built-in agent identity used by maintainer jobs. It is a member of every team."""
    agent_id = create_actor(conn, "db4ai-maintainer", kind="agent")
    conn.execute(
        "insert into team_members (team_id, actor_id, role) select id, %s, 'agent' from teams"
        " on conflict do nothing", (agent_id,))
    return load_actor(conn, agent_id)
