"""FAREWELL-2 (2026-08-22): a hand-started session killed the hard way must read
EXITED, not "watcher quiet". The watcher already knew how to observe the session
and file the farewell; it died in the same shot because it shared the session's
process group. Now: own session (spawn test lives in test_watch_v2_spawn), SIGHUP
handled like SIGTERM, and an orphaned watcher OBSERVES before it exits.
Every farewell is still a definite "process is gone" — never a silence."""
import signal

import pytest

from engram_mcp import inbox_wait as iw


class _Client:
    def __init__(self):
        self.farewells = []

    async def presence_farewell(self, **kw):
        self.farewells.append(kw)


@pytest.mark.asyncio
async def test_orphan_observes_and_farewells_when_the_session_is_positively_gone(monkeypatch):
    c = _Client()
    calls = []
    def gone(pid, start):
        calls.append((pid, start))
        return True
    monkeypatch.setattr(iw, "derive_project_name", lambda d: "proj")
    ok = await iw._observe_exit_then_farewell(
        c, (4242, "start-stamp"), "proj-claude-2@host", "/tmp/proj",
        window=2.0, poll=0.01, is_gone=gone)
    assert ok is True
    assert len(calls) >= 2, "two consecutive positive observations are required"
    assert c.farewells == [{"identity": "proj-claude-2", "project": "proj",
                            "project_dir": "/tmp/proj"}]


@pytest.mark.asyncio
async def test_orphan_exits_quietly_when_the_session_is_still_alive():
    """The bridge died alone (a respawn brings a new watcher): no farewell —
    a live session must never be reported exited."""
    c = _Client()
    ok = await iw._observe_exit_then_farewell(
        c, (4242, "s"), "proj-claude-2@host", "/tmp/proj",
        window=0.05, poll=0.01, is_gone=lambda *_: False)
    assert ok is False and c.farewells == []


@pytest.mark.asyncio
async def test_orphan_never_farewells_on_a_flapping_or_unaskable_probe():
    """A single 'gone' (ps hiccup, recycled pid) is not two; an exception from
    the probe counts as 'could not ask', which is never 'gone'."""
    c = _Client()
    seq = iter([True, False, True, False, True, False, True, False])
    ok = await iw._observe_exit_then_farewell(
        c, (1, "s"), "x@h", None, window=0.05, poll=0.005,
        is_gone=lambda *_: next(seq, False))
    assert ok is False and c.farewells == []
    def boom(*_):
        raise OSError("ps failed")
    ok = await iw._observe_exit_then_farewell(
        c, (1, "s"), "x@h", None, window=0.03, poll=0.005, is_gone=boom)
    assert ok is False and c.farewells == []


@pytest.mark.asyncio
async def test_orphan_with_no_identified_session_is_never_a_farewell():
    c = _Client()
    ok = await iw._observe_exit_then_farewell(
        c, None, "x@h", None, window=0.01, poll=0.005, is_gone=lambda *_: True)
    assert ok is False and c.farewells == []
    ok = await iw._observe_exit_then_farewell(
        c, (1, "s"), None, None, window=0.01, poll=0.005, is_gone=lambda *_: True)
    assert ok is False and c.farewells == []


def test_sighup_is_handled_like_sigterm_observe_then_farewell_or_gasp():
    """A closed terminal / hung-up tty delivers SIGHUP; unhandled, it was a
    silent death with no observation. Now it raises a SystemExit the main()
    path recognizes by code, same as SIGTERM."""
    prev_term = signal.getsignal(signal.SIGTERM)
    prev_hup = signal.getsignal(signal.SIGHUP)
    try:
        iw._install_signal_handlers()
        assert signal.getsignal(signal.SIGTERM) is iw._on_sigterm
        assert signal.getsignal(signal.SIGHUP) is iw._on_sighup
        with pytest.raises(SystemExit) as e:
            iw._on_sighup(signal.SIGHUP, None)
        assert e.value.code == 129 and iw._SIGNAL_EXIT_CODES[129] == "SIGHUP"
        with pytest.raises(SystemExit) as e:
            iw._on_sigterm(signal.SIGTERM, None)
        assert e.value.code == 143 and iw._SIGNAL_EXIT_CODES[143] == "SIGTERM"
    finally:
        signal.signal(signal.SIGTERM, prev_term)
        signal.signal(signal.SIGHUP, prev_hup)
