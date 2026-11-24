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


def _eval(a) -> None:
    from . import evals
    from .config import settings
    from .llm import ClaudeLLM, HeuristicLLM

    try:
        cases = evals.load_cases(a.cases)
    except (OSError, ValueError) as e:
        sys.exit(f"bad eval file: {e}")
    s = settings()
    llm = {"heuristic": HeuristicLLM, "claude": lambda: ClaudeLLM(s.model)}.get(a.llm, get_llm)()
    dup = a.dup_threshold if a.dup_threshold is not None else s.dup_threshold
    r = evals.run(cases, llm, get_embedder(), dup)
    _out(r)
    if a.min_precision is not None and any(t["precision"] < a.min_precision for t in r["tasks"].values()):
        sys.exit(1)


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
    u.add_argument("--email", help="the user's email, so connectors can map a source's users (e.g. Drive sharing)")

    em = sub.add_parser("email", help="map an existing actor to an email address (no new token)")
    em.add_argument("name")
    em.add_argument("address", nargs="?", help="omit with --clear to remove the mapping")
    em.add_argument("--clear", action="store_true")

    i = sub.add_parser("import", help="import a file or folder")
    i.add_argument("path", type=Path)
    i.add_argument("--team", required=True)
    i.add_argument("--as", dest="actor", help="actor name to attribute the import to")
    i.add_argument("--type", default="document", help="item type for text files (default: document)")
    i.add_argument("--vault", action="store_true", help="treat as a Markdown/Obsidian memory vault")

    sy = sub.add_parser("sync", help="incremental sync from a connector")
    sy.add_argument("connector", choices=["transcripts", "gdrive"])
    sy.add_argument("path", type=Path, nargs="?",
                    help="transcripts: folder of .vtt/.srt/.txt files (gdrive reads DB4AI_GDRIVE_* from the env)")
    sy.add_argument("--team", required=True)
    sy.add_argument("--as", dest="actor", help="actor name to attribute the sync to")
    sy.add_argument("--source", help="cursor name (default: the connector name)")
    sy.add_argument("--folder", action="append", dest="folders",
                    help="gdrive: sync only this folder or shared drive id and everything below it (repeatable; "
                         "default DB4AI_GDRIVE_FOLDERS, else all the token can read)")
    sy.add_argument("--readers", action="store_true", default=None,
                    help="gdrive: restrict each synced file to the team members it is shared with, matched by "
                         "email (default DB4AI_GDRIVE_READERS)")

    nr = sub.add_parser("notify-route", help="route a team's pending harness decisions to a webhook held in an env var")
    nr.add_argument("op", choices=["add", "remove", "list"])
    nr.add_argument("team")
    nr.add_argument("env_name", nargs="?", help="DB4AI_WEBHOOK_<NAME>: the env var holding the webhook URL")
    nr.add_argument("--as", dest="actor", required=True, help="an admin, or an owner of the team")

    m = sub.add_parser("maintain", help="run maintainer jobs")
    m.add_argument("--once", action="store_true", help="drain the queue and exit")

    ev = sub.add_parser("eval", help="score the dedup and staleness maintainers on a labelled set (no database)")
    ev.add_argument("--cases", type=Path, help="your own eval file (default: the built-in synthetic set)")
    ev.add_argument("--llm", choices=["heuristic", "claude"], help="override DB4AI_LLM (claude uses your API key)")
    ev.add_argument("--dup-threshold", type=float, help="override DB4AI_DUP_THRESHOLD")
    ev.add_argument("--min-precision", type=float, help="exit 1 if any task's precision is below this")

    sub.add_parser("serve", help="HTTP API + harness page").add_argument("--port", type=int, default=8000)
    sub.add_parser("mcp", help="MCP server over stdio (set DB4AI_TOKEN)")

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if a.cmd == "mcp":
        from .mcp_server import main as mcp_main
        return mcp_main()
    if a.cmd == "eval":
        return _eval(a)
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
            try:
                uid = auth.create_actor(conn, a.name, "agent" if a.agent else "human", a.admin, a.email)
            except ValueError as e:
                sys.exit(str(e))
            for spec in a.team:
                name, _, role = spec.partition(":")
                auth.add_member(conn, auth.create_team(conn, name), uid, role or "member")
            token = auth.issue_token(conn, uid)
        _out({"actor_id": uid, "token": token, "note": "store this token now; it is not shown again"})
    elif a.cmd == "email":
        if a.clear == bool(a.address):
            sys.exit("give an address, or --clear")
        try:
            with conn.transaction():
                auth.set_email(conn, a.name, a.address)
        except KeyError:
            sys.exit(f"no actor named {a.name!r}")
        except ValueError as e:
            sys.exit(str(e))
        except Exception as e:  # unique violation: another actor has it
            if getattr(e, "sqlstate", None) == "23505":
                sys.exit(f"{a.address} is already mapped to another actor")
            raise
        _out({"actor": a.name, "email": None if a.clear else auth.normalize_email(a.address)})
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
    elif a.cmd == "sync":
        from . import connectors

        with conn.transaction():
            actor = _actor(conn, a.actor)
        team_id = auth.team_id_by_name(conn, a.team)
        if a.connector == "gdrive":
            from .gdrive import DriveError, GoogleDrive
            try:
                _out(connectors.run(conn, actor, a.source or a.connector, team_id,
                                    GoogleDrive(folders=a.folders, readers=a.readers)))
            except DriveError as e:
                sys.exit(str(e))
            return
        if a.path is None or not a.path.is_dir():
            sys.exit(f"{a.path} is not a folder")
        _out(connectors.run(conn, actor, a.source or a.connector, team_id, connectors.TranscriptFolder(a.path)))
    elif a.cmd == "notify-route":
        from . import service

        with conn.transaction():
            actor = _actor(conn, a.actor)
        if a.op != "list" and not a.env_name:
            sys.exit("env_name is required")
        fn = {"add": service.add_notify_route, "remove": service.remove_notify_route}.get(a.op)
        try:
            _out(fn(conn, actor, a.team, a.env_name) if fn else service.notify_routes(conn, actor, a.team))
        except (KeyError, ValueError, auth.AccessDenied) as e:
            sys.exit(str(e))
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
