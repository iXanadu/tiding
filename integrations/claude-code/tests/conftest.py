import pytest
import respx

import engram_mcp.config as config
import engram_mcp.identity as identity
from engram_mcp.client import MemoryClient
from engram_mcp.identity import reset_session_pin


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch):
    """Pin bridge settings to deterministic values for every test.

    Settings load from the developer's real ~/.config/engram/identity (or an
    ENGRAM_IDENTITY-selected file) at import time — without this pin the suite
    would assert against whatever that box's live credentials say (this bit us
    when the identity file's namespace flipped fleet-ward and stale assertions
    kept passing/failing by machine, not by code).
    """
    monkeypatch.setattr(config.settings, "memory_api_url", "http://localhost:8920")
    monkeypatch.setattr(config.settings, "memory_namespace", "fleet")
    monkeypatch.setattr(config.settings, "memory_read_namespaces", "")
    monkeypatch.setattr(config.settings, "memory_default_scope", "machine")


@pytest.fixture(autouse=True)
def _reset_identity_pin(monkeypatch):
    """Isolate the session-scoped identity anchors before each test.

    Both are module globals (session-scoped in production, a single process
    across a test run), so without this an earlier test's state would leak:
      - the explicit override pin (_SESSION_PROJECT_DIR) is cleared, and
      - the startup-cwd anchor (_STARTUP_CWD) is neutralized to None, so a test
        that omits project_dir reproduces the pre-anchor ``admin`` default
        unless it explicitly opts into a cwd by setting identity._STARTUP_CWD.
    """
    reset_session_pin()
    monkeypatch.setattr(identity, "_STARTUP_CWD", None)
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
