import os
import sys

# Test THIS tree, not whichever tree the shared venv's editable install
# points at. Dev (~/projects/engram) and prod (/opt/srv/engram) share
# cc-memory-3.12 and only one holds the `pip install -e` mapping — found
# 2026-08-21 when a green run here had exercised prod's code while dev's
# edits sat untested. src/ goes FIRST on sys.path before any engram_mcp import.
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
for _m in [m for m in list(sys.modules) if m == "engram_mcp" or m.startswith("engram_mcp.")]:
    del sys.modules[_m]

import pytest
import respx

import engram_mcp.client as client_mod
import engram_mcp.config as config
import engram_mcp.identity as identity
from engram_mcp.client import MemoryClient
from engram_mcp.identity import reset_session_pin


@pytest.fixture(autouse=True)
def _no_real_watcher_in_tests(monkeypatch):
    """The bridge's heartbeat lazily starts a REAL watcher supervisor thread
    (real FIFO under ~/.local/state/engram/wake/, real child polling the
    real store as whatever seat this cwd resolves to). In a test process that
    is residue, not coverage — found 2026-08-21 as a row of auto-macmini-*
    FIFOs in the wake dir and a banner leaking into an exact-match test.
    Kill-switch it for every test; a test that wants the supervisor sets the
    env back itself."""
    import engram_mcp.server as _srv
    monkeypatch.setenv("ENGRAM_BRIDGE_WATCHER", "off")
    monkeypatch.setattr(_srv, "_WATCH_STATE",
                        {"state": None, "checked": 0.0, "seat": None})


@pytest.fixture(autouse=True)
def _quiet_id1_announce(monkeypatch):
    """ID-1's admin-fallthrough banner is a ONCE-PER-PROCESS announcement, so
    which test 'consumed' it depended on collection order and which tree was
    imported — test_memory_search_empty's exact-match failed when run alone
    or under the src/ pin. Default it to already-announced; the ID-1 tests
    reset it themselves."""
    import engram_mcp.server as _srv
    monkeypatch.setattr(_srv, "_ADMIN_FALLBACK_ANNOUNCED", True)


@pytest.fixture(autouse=True)
def _reset_server_time(monkeypatch):
    """TIME-1's captured server clock is module state set by ANY client call —
    without a reset, one test's mocked Date header prefixes every later
    test's banner (found live: test_identity leaked into test_server's
    exact-match assertions)."""
    monkeypatch.setattr(client_mod, "_LAST_SERVER_TIME", None)


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
    # Launch-env addressing must not leak into the suite. A session launched by
    # a launcher carries ENGRAM_CHANNELS (appends channels to every computed
    # listen_set), ENGRAM_INBOX_IDENTITY (replaces the computed seat),
    # ENGRAM_SESSION_KEY (names the seat file) and ENGRAM_PROVIDER — every one
    # of which changes what compute_identity() returns. Scrubbing only channels
    # meant the suite PASSED on a bare terminal and FAILED with 14 identity
    # errors inside an agent session, purely from the environment: the result
    # depended on who started the shell rather than on the code. Tests that
    # exercise these set them explicitly.
    for _var in (
        "ENGRAM_CHANNELS",
        identity.INBOX_IDENTITY_ENV,
        identity.SESSION_KEY_ENV,
        "ENGRAM_PROVIDER",
    ):
        monkeypatch.delenv(_var, raising=False)


@pytest.fixture(autouse=True)
def _isolate_seat_files(monkeypatch, tmp_path):
    """Never let a test touch a REAL session's seat file.

    ``take_seat()`` writes an actual file, and the path is keyed on the session
    key — which is inherited from the environment when a launcher set one, and
    otherwise derived from this process's harness parent. So a suite run INSIDE
    a live agent session would rewrite that session's own seat: its bridge and
    its watcher both re-resolve identity from that file, so both would silently
    start answering to whatever the last test set, at another project's address.

    That is not hypothetical — it happened during development of SEAT-3, and
    the symptom was a compute_identity assertion failing with a completely
    unrelated project's name. Autouse and unconditional: the guarantee has to
    hold for every test, including ones that never mention seats.
    """
    monkeypatch.setenv(identity.SEATS_DIR_ENV, str(tmp_path / "seats"))
    identity._DISCOVERED_SEAT_PATH = None
    yield
    identity._DISCOVERED_SEAT_PATH = None


@pytest.fixture(autouse=True)
def _reset_principal_cache():
    """Isolate the /whoami principal cache between tests.

    All three are module globals with process lifetime. Without a reset, the
    first test to resolve a project partition decides what every later test
    sees: a latched success leaks a principal name, and (pre-PART-1) a latched
    failure leaked user_id='unknown'. The retry deadline is monotonic-clock
    based, so a failure in one test could also silently suppress the /whoami
    attempt of a test running within the retry window.
    """
    import engram_mcp.server as server_mod

    def _reset():
        server_mod._PRINCIPAL_CACHE = None
        server_mod._PRINCIPAL_FETCHED = False
        server_mod._PRINCIPAL_RETRY_AT = 0.0

    _reset()
    yield
    _reset()


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
