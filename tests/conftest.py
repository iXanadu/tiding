import asyncpg
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from server.config import settings
from server.db import init_pool, close_pool
from server.embeddings import init_client, close_client

TEST_DB_NAME = "engram_test"


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
    await init_pool()
    await init_client()
    yield
    await close_client()
    await close_pool()


@pytest_asyncio.fixture
async def client(services):
    """Async HTTP client wired to the FastAPI app (with services initialized)."""
    from server.main import app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def db_pool(services):
    """Raw database pool for direct SQL in tests."""
    from server.db import get_pool
    return await get_pool()
