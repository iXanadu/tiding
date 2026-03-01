import pytest
import respx

from engram_mcp.client import MemoryClient


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
