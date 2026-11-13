"""`db4ai` command line."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import auth
from .blobs import open_store
from .db import connect, migrate
from .embeddings import get_embedder
from .llm import get_llm


def _out(v) -> None:
    from .service import jsonable

    print(json.dumps(jsonable(v), indent=2, default=str))


def _actor(conn, name: str | None):
    if name:
        row = conn.execute("select id from actors where name=%s", (name,)).fetchone()
        if row is None:
            sys.exit(f"no actor named {name!r}")
        return auth.load_actor(conn, row["id"])
    return auth.maintainer_agent(conn)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="db4ai", description="Self-maintaining database for Human+AI data")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="create/upgrade the schema")

    t = sub.add_parser("team", help="create a team")
    t.add_argument("name")

    u = sub.add_parser("user", help="create a user (or agent) and print an API token")
    u.add_argument("name")
    u.add_argument("--team", action="append", default=[], help="team[:role], role=owner|member (repeatable)")
    u.add_argument("--admin", action="store_true")
    u.add_argument("--agent", action="store_true")

    i = sub.add_parser("import", help="import a file or folder")
    i.add_argument("path", type=Path)
    i.add_argument("--team", required=True)
    i.add_argument("--as", dest="actor", help="actor name to attribute the import to")
    i.add_argument("--type", default="document", help="item type for text files (default: document)")
    i.add_argument("--vault", action="store_true", help="treat as a Markdown/Obsidian memory vault")

    m = sub.add_parser("maintain", help="run maintainer jobs")
    m.add_argument("--once", action="store_true", help="drain the queue and exit")

    sub.add_parser("serve", help="HTTP API + harness page").add_argument("--port", type=int, default=8000)
    sub.add_parser("mcp", help="MCP server over stdio (set DB4AI_TOKEN)")

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if a.cmd == "mcp":
        from .mcp_server import main as mcp_main
        return mcp_main()
    if a.cmd == "serve":
        import uvicorn

        from .api import create_app
        return uvicorn.run(create_app(), host="0.0.0.0", port=a.port)

    conn = connect()
    if a.cmd == "migrate":
        _out({"applied": migrate(conn)})
    elif a.cmd == "team":
        with conn.transaction():
            _out({"team_id": auth.create_team(conn, a.name)})
    elif a.cmd == "user":
        with conn.transaction():
            uid = auth.create_actor(conn, a.name, "agent" if a.agent else "human", a.admin)
            for spec in a.team:
                name, _, role = spec.partition(":")
                auth.add_member(conn, auth.create_team(conn, name), uid, role or "member")
            token = auth.issue_token(conn, uid)
        _out({"actor_id": uid, "token": token, "note": "store this token now; it is not shown again"})
    elif a.cmd == "import":
        from . import ingest

        with conn.transaction():
            actor = _actor(conn, a.actor)
        team_id = auth.team_id_by_name(conn, a.team)
        blobs = open_store()
        if a.path.is_dir():
            fn = ingest.import_vault if a.vault else ingest.import_folder
            kwargs = {} if a.vault else {"item_type": a.type}
            r = fn(conn, actor, team_id, a.path, blobs=blobs, **kwargs)
            r.pop("results")
        else:
            r = ingest.import_file(conn, actor, team_id, a.path, blobs=blobs, item_type=a.type)
        _out(r)
    elif a.cmd == "maintain":
        from .maintainers import Context
        from .worker import run_forever, run_until_idle, schedule_periodic

        ctx = Context(conn, get_llm(), get_embedder(), open_store())
        if a.once:
            schedule_periodic(conn)
            _out(run_until_idle(ctx))
        else:
            run_forever(ctx)


if __name__ == "__main__":
    main()
