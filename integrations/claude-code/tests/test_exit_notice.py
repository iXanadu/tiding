"""EXIT-NOTICE-1 (2026-08-21): on a clean exit the bridge certifies its own
session's death, so a hand-started agent reads EXITED on the roster like an
AgentBeast-spawned one — no skill, nothing for the agent to remember.

Measured the same day: the harness closes the bridge's stdin on /exit, the
MCP loop returns, atexit runs 0.1s later. These tests pin the hook's
contract: posts the LANE-4 certificate for the GRANTED seat with
cause="session-exit", graceful=True; never posts when no seat was ever held;
never raises.
"""

import pytest

from engram_mcp import server as srv


@pytest.fixture
def captured(monkeypatch):
    calls = []

    def _fake_post(url, token, payload):
        calls.append((url, token, payload))
        return True

    monkeypatch.setattr(srv, "_post_exit_notice_sync", _fake_post)
    monkeypatch.setattr(srv.settings, "memory_api_token", "engram_test")
    monkeypatch.setattr(srv.settings, "memory_api_url", "http://store.test:8920")
    return calls


def test_exit_notice_posts_certificate_for_granted_seat(captured, monkeypatch):
    monkeypatch.setattr(srv, "_EXIT_NOTICE", {
        "session_key": "auto-claude-abc123", "seat": "proj-claude-3",
        "lane": "proj-claude", "project": "proj", "provider": "claude",
        "host": "macmini",
    })
    srv._exit_notice()
    assert len(captured) == 1
    url, token, payload = captured[0]
    assert url == "http://store.test:8920" and token == "engram_test"
    assert payload["session_key"] == "auto-claude-abc123"
    assert payload["seat"] == "proj-claude-3"           # the GRANTED seat, ordinal kept
    assert payload["lane"] == "proj-claude"
    assert payload["project"] == "proj"
    assert payload["provider"] == "claude"
    assert payload["host"] == "macmini"
    assert payload["cause"] == "session-exit"
    assert payload["graceful"] is True
    assert payload["died_at"].endswith("+00:00") or payload["died_at"].endswith("Z")


def test_no_seat_no_notice(captured, monkeypatch):
    # never seated (store unreachable all session, or claims refused) → nothing
    # to certify, and the hook must not INVENT a seat to certify
    monkeypatch.setattr(srv, "_EXIT_NOTICE", {})
    srv._exit_notice()
    assert captured == []
    monkeypatch.setattr(srv, "_EXIT_NOTICE", {"seat": "proj-claude-3"})  # no key
    srv._exit_notice()
    assert captured == []


def test_no_token_no_notice(captured, monkeypatch):
    monkeypatch.setattr(srv.settings, "memory_api_token", "")
    monkeypatch.setattr(srv, "_EXIT_NOTICE", {
        "session_key": "k", "seat": "proj-claude-3", "project": "proj",
    })
    srv._exit_notice()
    assert captured == []


def test_exit_notice_never_raises(monkeypatch):
    def _boom(url, token, payload):
        raise RuntimeError("store down")

    monkeypatch.setattr(srv, "_post_exit_notice_sync", _boom)
    monkeypatch.setattr(srv.settings, "memory_api_token", "engram_test")
    monkeypatch.setattr(srv, "_EXIT_NOTICE", {
        "session_key": "k", "seat": "proj-claude-3", "project": "proj",
    })
    srv._exit_notice()  # must not raise — a goodbye never breaks a clean exit


def test_exit_hook_is_registered_to_run_before_watcher_kill():
    # atexit is LIFO: registered AFTER _kill_watcher_child means it runs FIRST,
    # so the notice posts while the process is still fully alive.
    import atexit
    # atexit has no public registry; assert by source order instead.
    import inspect
    src = inspect.getsource(srv)
    assert src.index("_atexit.register(_kill_watcher_child)") < src.index("_atexit.register(_exit_notice)")
