import pytest
import respx

from engram_mcp.client import MemoryClient
from engram_mcp.identity import reset_session_pin


@pytest.fixture(autouse=True)
def _reset_identity_pin():
    """Clear the session project_dir pin before each test.

    The pin is a module global (session-scoped in production, but a single
    process across a test run), so without this reset an earlier test's
    project_dir would leak into a later test that omits one.
    """
    reset_session_pin()
    yield
    reset_session_pin()


@pytest.fixture
def mock_api():
    """Provide a respx mock router for the memory API."""
    with respx.mock(base_url="http://localhost:8920") as mock:
        yield mock


@pytest.fixture
async def client(mock_api):
    """Provide a MemoryClient that uses the mocked transport."""
    c = MemoryClient("http://localhost:8920")
    yield c
    await c.close()
