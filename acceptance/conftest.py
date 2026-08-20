"""ACCEPT-1 harness fixtures: a real server on a scratch port + scratch DB.

Deliberately NOT part of the unit suite (`pytest tests/`): this spawns
processes and takes tens of seconds. Run via `scripts/accept.sh` or
`pytest acceptance/`.

R-c: every fixture that creates world state asserts residue == NONE on the
way out — a harness that litters the register becomes the next session's
ghost.
"""

from __future__ import annotations

import os
import pathlib
import socket
import subprocess
import sys
import time

import asyncpg
import httpx
import pytest

from .driver import ACCEPT_TOKEN

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ACCEPT_DB = "engram_accept"

_AUTH = {"Authorization": f"Bearer {ACCEPT_TOKEN}"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _ensure_db():
    sys.path.insert(0, str(REPO_ROOT))
    from server.config import settings

    admin_dsn = (
        f"postgresql://{settings.db_user}"
        + (f":{settings.db_password}" if settings.db_password else "")
        + f"@{settings.db_host}:{settings.db_port}/postgres"
    )
    conn = await asyncpg.connect(admin_dsn)
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", ACCEPT_DB
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{ACCEPT_DB}"')
    finally:
        await conn.close()
    dsn = (
        f"postgresql://{settings.db_user}"
        + (f":{settings.db_password}" if settings.db_password else "")
        + f"@{settings.db_host}:{settings.db_port}/{ACCEPT_DB}"
    )
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    finally:
        await conn.close()
    return dsn


@pytest.fixture(scope="session")
def accept_server():
    """A real uvicorn server, this checkout, scratch DB, scratch port."""
    import asyncio

    dsn = asyncio.run(_ensure_db())
    port = _free_port()
    env = {
        **os.environ,
        "ENGRAM_DB_NAME": ACCEPT_DB,
        # PROD-SHAPED, not merely convenient: require_auth=true makes the
        # server bootstrap a '_bootstrap' admin principal from
        # ENGRAM_API_TOKEN. Legacy-token mode looked simpler but 401s on
        # every namespace-resolving read (resolve_read_namespaces correctly
        # refuses an anonymous caller) — the shape prod actually runs is
        # also the shape that works.
        "ENGRAM_REQUIRE_AUTH": "true",
        "ENGRAM_API_TOKEN": ACCEPT_TOKEN,
        "ENGRAM_PORT": str(port),
        # Loopback in CONFIG, not just on the uvicorn arg: the bind-security
        # guard reads settings.host, and an open store on 0.0.0.0 is exactly
        # what it exists to refuse.
        "ENGRAM_HOST": "127.0.0.1",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 90  # first boot loads the embedding model
    last_err = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"accept server died on boot:\n{proc.stdout.read()}"
            )
        try:
            r = httpx.get(f"{url}/health", timeout=2)
            if r.status_code == 200 and r.json().get("status") == "ok":
                break
        except Exception as e:  # noqa: BLE001 — boot poll
            last_err = e
        time.sleep(0.5)
    else:
        proc.kill()
        raise RuntimeError(f"accept server never became healthy: {last_err}")

    yield {"url": url, "dsn": dsn, "proc": proc}

    proc.kill()
    proc.wait(timeout=15)


@pytest.fixture()
def registry(accept_server):
    """Direct world-observation helpers against the scratch server."""

    class Registry:
        url = accept_server["url"]

        def seats(self, project: str) -> list[dict]:
            r = httpx.post(f"{self.url}/session/seats",
                           json={"project": project}, headers=_AUTH, timeout=15)
            r.raise_for_status()
            return r.json()["seats"]

        def release(self, session_key: str, project: str) -> str | None:
            r = httpx.post(f"{self.url}/session/release",
                           json={"session_key": session_key,
                                 "project": project}, headers=_AUTH, timeout=15)
            r.raise_for_status()
            return r.json().get("released")

        def send(self, to: str, subject: str, body: str) -> str:
            r = httpx.post(f"{self.url}/memory/send",
                           json={"to": to, "subject": subject, "body": body},
                           headers=_AUTH, timeout=15)
            r.raise_for_status()
            return r.json()["id"]

        def residue(self, marker: str) -> list[tuple]:
            """Anything left in the store that mentions the run marker."""
            import asyncio

            async def _q():
                conn = await asyncpg.connect(accept_server["dsn"])
                try:
                    return await conn.fetch(
                        """
                        SELECT scope, user_id, project, key FROM memories
                        WHERE project LIKE $1 OR user_id LIKE $1
                        """,
                        f"%{marker}%",
                    )
                finally:
                    await conn.close()

            return [tuple(r) for r in asyncio.run(_q())]

        def purge(self, marker: str) -> int:
            import asyncio

            async def _q():
                conn = await asyncpg.connect(accept_server["dsn"])
                try:
                    return await conn.execute(
                        """
                        DELETE FROM memories
                        WHERE project LIKE $1 OR user_id LIKE $1
                        """,
                        f"%{marker}%",
                    )
                finally:
                    await conn.close()

            out = asyncio.run(_q())
            return int(out.split()[-1])

    return Registry()


# ─────────────────────────────────────────────────────────────────────────────
# UNRUNNABLE MUST NOT EXIT 0.
#
# Adopted from agentbeast-app-grok-2's audit of AgentBeast's arrival matrix
# (huddle DfNRCl6x, 2026-08-20), applied here because the criticism lands on
# THIS suite identically and nobody had said so: "printing OWED WORK and
# returning 0 is the reader census in a new costume. A missing capability is
# not a FAIL row; it is also not a green badge."
#
# An UNRUNNABLE arrival row means a claim we cannot observe — the exact state
# that let 10c ship. If the runner exits 0 on it, the gap reads as coverage,
# which is this suite's own disease turned on itself. Distinct exit code 2:
# not a regression (that is 1), not clean (that is 0), OWED.
# ─────────────────────────────────────────────────────────────────────────────

_UNRUNNABLE_MARK = "UNRUNNABLE"


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    owed = [
        r for r in terminalreporter.stats.get("skipped", [])
        if _UNRUNNABLE_MARK in str(getattr(r, "longrepr", "") or "")
    ]
    if not owed:
        return
    terminalreporter.write_sep("=", "OWED WORK — unrunnable arrival claims", red=True)
    for r in owed:
        terminalreporter.write_line(f"  · {r.nodeid}")
    terminalreporter.write_line(
        "\nThese rows name a claim this harness CANNOT observe. That is not a "
        "pass and not a failure — it is coverage we do not have. Exiting 2 so "
        "it cannot be mistaken for green."
    )
    if exitstatus == 0:
        session = config.pluginmanager.get_plugin("session")
        if session is not None:
            session.exitstatus = 2
