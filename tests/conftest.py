import pathlib

import asyncpg
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import server
from server.config import settings
from server.db import init_pool, close_pool
from server.embeddings import init_client, close_client

TEST_DB_NAME = "engram_test"

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _assert_testing_this_checkout() -> None:
    """TEST-1: fail loudly if `server` resolves outside this repo.

    Dev (`~/projects/engram`) and prod (`/opt/srv/engram`) are both
    `pip install -e` into the same pyenv virtualenv, so `server.*` resolves to
    whichever was installed LAST. On 2026-07-27 that was prod: pytest reported
    255 passed while importing `/opt/srv/engram`, and edits to the working
    tree had no effect on any run.

    That is worse than a stale import, because it defeats falsification. A fix
    was run with its change stashed (failed) and restored (failed) — the same
    result twice, which only read as suspicious because the failure was
    inspected. Had the existing code happened to satisfy the new test, the
    sequence would have read as a clean PASS for a change that was never
    loaded. Green tests for code you did not write, with nothing in the output
    naming which tree it used.

    A comment in CLAUDE.md cannot fix a silent failure — the remedy
    (`pip install -e .` from the dev dir) has to be applied BEFORE you know
    you need it. So assert it on every run instead.
    """
    resolved = pathlib.Path(server.__file__).resolve()
    try:
        resolved.relative_to(_REPO_ROOT)
    except ValueError:
        raise RuntimeError(
            "\n"
            "══════════════════════════════════════════════════════════════\n"
            " TEST-1: pytest is testing a DIFFERENT CHECKOUT than this one.\n"
            "══════════════════════════════════════════════════════════════\n"
            f"  tests live in : {_REPO_ROOT}\n"
            f"  `server` came from: {resolved.parent}\n"
            "\n"
            "  Dev and prod are both editable-installed into this virtualenv\n"
            "  and the other one won. Every result from this run would\n"
            "  describe code you are not editing — including any test you\n"
            "  just 'verified failing against the old code'.\n"
            "\n"
            "  Fix:  pip install -e .   (from this repo root)\n"
            "══════════════════════════════════════════════════════════════"
        )


_assert_testing_this_checkout()


async def _ensure_test_db_exists() -> None:
    """Create the test database if missing. Tests are destructive (DELETE
    fixtures, raw INSERTs) and MUST NOT run against the production DB.

    Connects to the 'postgres' maintenance DB to issue CREATE DATABASE,
    then installs the pgvector extension on the new DB (asyncpg's
    register_vector codec fails to introspect the type otherwise).
    """
    admin_dsn = (
        f"postgresql://{settings.db_user}"
        + (f":{settings.db_password}" if settings.db_password else "")
        + f"@{settings.db_host}:{settings.db_port}/postgres"
    )
    conn = await asyncpg.connect(admin_dsn)
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", TEST_DB_NAME
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    finally:
        await conn.close()

    # Install pgvector on the test DB before the pool starts handing out
    # connections (init_connection registers the vector codec on every
    # connection, which requires the extension to already exist).
    test_dsn = (
        f"postgresql://{settings.db_user}"
        + (f":{settings.db_password}" if settings.db_password else "")
        + f"@{settings.db_host}:{settings.db_port}/{TEST_DB_NAME}"
    )
    conn = await asyncpg.connect(test_dsn)
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    finally:
        await conn.close()


@pytest_asyncio.fixture(scope="session")
async def services():
    """Initialize DB pool and embedding client once for the entire test session.

    Forces all tests to run against ``engram_test``, not the production
    ``engram`` DB. The fixture creates the test DB on first use; schema +
    migrations run on connect via init_pool.

    Tests are destructive (``_cleanup_inbox`` etc.) and would obliterate
    real data if pointed at prod — see lesson/tests-shared-db-destructive.
    The assertion below is a hard safety rail.
    """
    if settings.db_name != TEST_DB_NAME:
        await _ensure_test_db_exists()
        settings.db_name = TEST_DB_NAME
    assert settings.db_name == TEST_DB_NAME, (
        f"Refusing to run destructive tests against {settings.db_name!r}. "
        f"Tests must target {TEST_DB_NAME!r}."
    )
    # Pin the baseline auth posture regardless of the developer's .env
    # (a dev ENGRAM_HOST=0.0.0.0 must not flip the anonymous-admin gate for
    # every plain-client test). Gate tests patch host/tokens explicitly.
    settings.host = "127.0.0.1"
    settings.api_token = ""
    settings.require_auth = False
    await init_pool()
    await init_client()
    yield
    await close_client()
    await close_pool()


@pytest_asyncio.fixture
async def client(services):
    """Async HTTP client wired to the FastAPI app (with services initialized)."""
    from server.main import app
    # base_url host must be in settings.trusted_hosts (TrustedHostMiddleware) —
    # 'localhost' is allowed by default, like a real loopback client.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


@pytest_asyncio.fixture
async def db_pool(services):
    """Raw database pool for direct SQL in tests."""
    from server.db import get_pool
    return await get_pool()
