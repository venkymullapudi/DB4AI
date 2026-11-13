"""Tests run against a real, throwaway Postgres cluster.

Set DB4AI_TEST_DATABASE_URL to use an existing server (it must allow CREATE DATABASE);
otherwise a temporary cluster is started with the local `initdb`/`pg_ctl`.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import uuid
from pathlib import Path

import psycopg
import pytest

from db4ai import auth
from db4ai.blobs import FileBlobStore
from db4ai.db import connect, migrate
from db4ai.embeddings import HashEmbedder
from db4ai.llm import HeuristicLLM
from db4ai.maintainers import Context

PG_BIN_CANDIDATES = ["/opt/homebrew/opt/postgresql@16/bin", "/usr/lib/postgresql/16/bin", "/usr/local/bin"]


def _pg_bin(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    for d in PG_BIN_CANDIDATES:
        if Path(d, name).exists():
            return str(Path(d, name))
    pytest.skip(f"{name} not found; set DB4AI_TEST_DATABASE_URL")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def server_url():
    if os.environ.get("DB4AI_TEST_DATABASE_URL"):
        yield os.environ["DB4AI_TEST_DATABASE_URL"]
        return
    tmp = Path(tempfile.mkdtemp(prefix="db4ai-pg-"))
    data, port = tmp / "data", _free_port()
    env = {**os.environ, "LC_ALL": "C", "LANG": "C"}  # macOS postmaster refuses to start without a locale
    subprocess.run([_pg_bin("initdb"), "-D", str(data), "-U", "postgres", "-A", "trust", "--no-sync",
                    "-E", "UTF8", "--locale=C"], check=True, capture_output=True, env=env)
    subprocess.run([_pg_bin("pg_ctl"), "-D", str(data), "-l", str(tmp / "log"), "-w",
                    "-o", f"-p {port} -k {tmp} -c listen_addresses='' -c fsync=off", "start"],
                   check=True, capture_output=True, env=env)
    try:
        yield f"postgresql://postgres@/postgres?host={tmp}&port={port}"
    finally:
        subprocess.run([_pg_bin("pg_ctl"), "-D", str(data), "-m", "immediate", "stop"], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(scope="session")
def template_db(server_url):
    """Migrate once into a template database; every test clones it."""
    with psycopg.connect(server_url, autocommit=True) as c:
        c.execute("drop database if exists db4ai_tpl")
        c.execute("create database db4ai_tpl")
    tpl_url = server_url.replace("/postgres?", "/db4ai_tpl?") if "?" in server_url else server_url.rsplit("/", 1)[0] + "/db4ai_tpl"
    with connect(tpl_url) as c:
        migrate(c)
    return "db4ai_tpl"


@pytest.fixture
def db_url(server_url, template_db):
    name = "t_" + uuid.uuid4().hex[:12]
    with psycopg.connect(server_url, autocommit=True) as c:
        c.execute(f"create database {name} template {template_db}")
    url = server_url.replace("/postgres?", f"/{name}?") if "?" in server_url else server_url.rsplit("/", 1)[0] + f"/{name}"
    yield url
    with psycopg.connect(server_url, autocommit=True) as c:
        c.execute(f"drop database if exists {name} with (force)")


@pytest.fixture
def conn(db_url, monkeypatch):
    monkeypatch.setenv("DB4AI_DATABASE_URL", db_url)
    c = connect(db_url)
    yield c
    c.close()


@pytest.fixture
def world(conn, tmp_path):
    """Two teams, an owner, a member, an outsider, the maintainer agent, and a maintainer context."""
    with conn.transaction():
        eng = auth.create_team(conn, "eng")
        sales = auth.create_team(conn, "sales")
        alice = auth.create_actor(conn, "alice")          # eng owner
        bob = auth.create_actor(conn, "bob")              # eng member
        carol = auth.create_actor(conn, "carol")          # sales member only
        root = auth.create_actor(conn, "root", is_admin=True)
        auth.add_member(conn, eng, alice, "owner")
        auth.add_member(conn, eng, bob, "member")
        auth.add_member(conn, sales, carol, "member")
        tokens = {n: auth.issue_token(conn, i) for n, i in (("alice", alice), ("bob", bob), ("carol", carol), ("root", root))}
    blobs = FileBlobStore(tmp_path / "blobs")
    ctx = Context(conn, HeuristicLLM(), HashEmbedder(), blobs)

    class W:
        pass

    w = W()
    w.conn, w.eng, w.sales, w.blobs, w.ctx, w.tokens = conn, eng, sales, blobs, ctx, tokens
    w.alice, w.bob, w.carol, w.root = (auth.load_actor(conn, x) for x in (alice, bob, carol, root))
    return w
