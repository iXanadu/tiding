"""The wake stream must survive its consumer (watch-claim v2 §Consumer,
corrected 2026-08-21).

Measured in production that morning: the session's Monitor reader died; the
next emit hit EPIPE; the watcher crashed (its gasp down the same dead pipe);
the supervisor's replacement blocked at open forever; the seat read `expired`
while the owner typed at a session that believed itself covered. These tests
pin the repaired contract: a detached reader is waited for again and the lost
line is re-sent; an orphaned watcher exits instead of blocking; the gasp tells
the agent to ATTACH, never to arm; the bridge banners a live session whose
stream is not covered.
"""
from __future__ import annotations

import json
import os
import threading
import time

import pytest

from engram_mcp import inbox_wait as iw


@pytest.fixture
def fifo(tmp_path, monkeypatch):
    path = str(tmp_path / "wake.fifo")
    os.mkfifo(path, 0o600)
    monkeypatch.setattr(iw, "_FIFO_PATH", path)
    monkeypatch.setattr(iw, "_FIFO_FD", None)
    monkeypatch.setattr(iw, "_FIFO_FILE", None)
    monkeypatch.setattr(iw, "_MIRROR_TO_STDOUT", False)  # leave pytest's fd 1 alone
    return path


def _read_line(fd: int, timeout: float = 5.0) -> str:
    buf = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            time.sleep(0.02)
            continue
        if chunk:
            buf += chunk
            if b"\n" in buf:
                return buf.decode()
        else:
            time.sleep(0.02)
    raise AssertionError(f"no line within {timeout}s; got {buf!r}")


def test_open_for_write_waits_for_a_reader_then_goes_blocking(fifo):
    got = {}
    def writer():
        got["f"] = iw._open_fifo_for_write(fifo)
    th = threading.Thread(target=writer); th.start()
    time.sleep(0.3)
    assert th.is_alive(), "open must not return before a reader exists (claim follows attach)"
    r = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    th.join(timeout=5); assert not th.is_alive()
    import fcntl
    assert not (fcntl.fcntl(got["f"].fileno(), fcntl.F_GETFL) & os.O_NONBLOCK), \
        "once attached the write end is blocking (a full pipe stalls, never drops)"
    got["f"].close(); os.close(r)


def test_emit_survives_consumer_detach_and_resends_the_lost_line(fifo):
    # attach reader #1, arm the write end the way _run does
    r1 = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    f = iw._open_fifo_for_write(fifo)
    iw._FIFO_FILE = f; iw._FIFO_FD = f.fileno()
    iw._out('{"id":"inbox/1"}')
    assert json.loads(_read_line(r1))["id"] == "inbox/1"

    # reader #1 dies (the Monitor cat-loop vanished). Next emit must NOT raise
    # and must NOT be lost: it blocks for reader #2 and re-sends.
    os.close(r1)
    done = threading.Event()
    def emit_two():
        iw._out('{"id":"inbox/2"}')
        done.set()
    th = threading.Thread(target=emit_two, daemon=True); th.start()
    time.sleep(0.5)
    assert not done.is_set(), "with no reader the emit waits (it does not crash)"
    r2 = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    assert done.wait(5), "emit completes once a new reader attaches"
    assert json.loads(_read_line(r2))["id"] == "inbox/2", "the line that hit EPIPE is re-sent"
    iw._out('{"id":"inbox/3"}')
    assert json.loads(_read_line(r2))["id"] == "inbox/3"
    os.close(r2)


def test_open_for_write_exits_instead_of_orphaning(fifo, monkeypatch):
    monkeypatch.setattr(iw, "_orphaned", lambda: True)
    t0 = time.monotonic()
    with pytest.raises(SystemExit):
        iw._open_fifo_for_write(fifo)   # no reader will ever come
    assert time.monotonic() - t0 < 5


def test_dying_gasp_in_fifo_mode_says_attach_never_arm(fifo, capsys):
    r = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    f = iw._open_fifo_for_write(fifo)
    iw._FIFO_FILE = f; iw._FIFO_FD = f.fileno()
    iw._dying_gasp("test")
    obj = json.loads(_read_line(r))
    assert obj["event"] == "watcher-dying"
    assert obj["command"].startswith("while true; do cat "), obj
    assert "engram-inbox-wait" not in obj["command"]
    assert "do NOT launch engram-inbox-wait" in obj["action"]
    assert "dying — test" in capsys.readouterr().err
    # and with NOBODY listening the gasp must not block or raise
    os.close(r)
    t0 = time.monotonic()
    iw._dying_gasp("nobody home")
    assert time.monotonic() - t0 < 2


def test_dying_gasp_legacy_mode_says_do_not_rearm(capsys, monkeypatch):
    monkeypatch.setattr(iw, "_FIFO_PATH", None)
    iw._dying_gasp("legacy")
    obj = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert obj["command"] == "memory_status"
    assert "Do NOT re-arm" in obj["action"]


def test_bridge_banner_only_when_uncovered_and_only_for_claude(monkeypatch):
    from engram_mcp import server as srv
    monkeypatch.setitem(srv._WATCHER_SUP, "fifo", "/tmp/x.fifo")
    monkeypatch.setattr(srv, "resolve_provider", lambda: "claude")
    for st in (None, "covered"):
        monkeypatch.setitem(srv._WATCH_STATE, "state", st)
        assert srv._wake_stream_banner() == ""
    monkeypatch.setitem(srv._WATCH_STATE, "state", "expired")
    monkeypatch.setitem(srv._WATCH_STATE, "seat", "proj-claude-2")
    b = srv._wake_stream_banner()
    assert b.startswith("⛔ WAKE STREAM NOT COVERED")
    assert "while true; do cat /tmp/x.fifo" in b
    assert "Never launch engram-inbox-wait" in b
    # a harness without a background stream tool is never handed a command
    # that does not return
    monkeypatch.setattr(srv, "resolve_provider", lambda: "grok")
    assert srv._wake_stream_banner() == ""
