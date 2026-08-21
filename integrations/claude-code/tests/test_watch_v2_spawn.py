"""watch-claim v2 step 2: the bridge-owned watcher spawn path.

These tests exercise the code that 360 green tests just proved they DON'T:
a missing `import sys` sat in the supervisor unnoticed because nothing ran
it. Import checks prove parsing; only execution proves execution.
"""

import asyncio
import os
import stat

import pytest

import engram_mcp.server as server
from engram_mcp.inbox_wait import _WatchClaimState, _open_fifo_for_write


def test_supervisor_references_resolve():
    """Every name the supervisor touches must exist at module scope — the
    exact defect found here (sys.executable, no import sys) survives import
    and the full suite, then NameErrors on first real spawn."""
    import inspect
    src = inspect.getsource(server._watcher_supervisor_thread)
    ns = vars(server)
    for name in ("sys", "os", "resolve_session_key"):
        assert name in ns, f"supervisor uses {name} but module doesn't bind it"
    assert "sys.executable" in src  # the line that was dead code


def test_fifo_open_blocks_until_reader_attaches(tmp_path):
    """The load-bearing ordering: claim follows attach, because the open
    cannot return before a reader exists. If this ever stops blocking, a
    FIFO nobody tails could claim coverage — F4 with extra steps."""
    import threading, time as _t
    fifo = str(tmp_path / "wake.fifo")
    opened = threading.Event()

    def writer():
        f = _open_fifo_for_write(fifo)
        opened.set()
        f.close()

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    _t.sleep(0.5)
    assert not opened.is_set(), (
        "open-for-write returned with NO reader attached — the claim-after-"
        "attach guarantee just silently vanished"
    )
    # attach a reader; the writer must now unblock
    fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    assert opened.wait(timeout=5), "writer never unblocked after attach"
    os.close(fd)
    assert stat.S_ISFIFO(os.stat(fifo).st_mode)


@pytest.mark.asyncio
async def test_claim_state_retries_on_held_and_stops_on_partial():
    """K1: held → retry on a timer (never exit-forever). F10: partial →
    unretryable exit code."""
    calls = []

    class FakeClient:
        def __init__(self, verdicts):
            self.verdicts = list(verdicts)

        async def watch_claim(self, **kw):
            calls.append(kw)
            v = self.verdicts.pop(0)
            if v == "held":
                return {"verdict": "held", "retry_after_seconds": 0.01,
                        "holder_armed_by": "ab"}
            if v == "partial":
                return {"verdict": "partial-refused", "reason": "no seat in set"}
            return {"verdict": "granted"}

    st = _WatchClaimState("proj-claude-2")
    rc = await st.acquire(FakeClient(["held", "held", "granted"]), "/tmp", ["proj-claude-2"])
    assert rc is None and st.held and len(calls) == 3, "held did not retry to grant"

    st2 = _WatchClaimState("proj-claude-2")
    rc2 = await st2.acquire(FakeClient(["partial"]), "/tmp", ["proj"])
    from engram_mcp.inbox_wait import EXIT_PARTIAL_CLAIM
    assert rc2 == EXIT_PARTIAL_CLAIM and not st2.held


@pytest.mark.asyncio
async def test_claim_api_unreachable_runs_unheld_not_dead():
    """K3: the repair crew's case. Claim API down → run UNHELD, and beats
    report holder (emission is legitimate) without ever touching the API."""
    class DeadClient:
        async def watch_claim(self, **kw):
            raise ConnectionError("store is sick")

        async def watch_beat(self, *a):
            raise AssertionError("unheld mode must not beat a dead API")

    st = _WatchClaimState("proj-claude-2")
    rc = await st.acquire(DeadClient(), "/tmp", ["proj-claude-2"])
    assert rc is None and st.unheld_mode and not st.held
    assert await st.beat(DeadClient()) == "holder"


@pytest.mark.asyncio
async def test_beat_three_verdicts_map_to_three_behaviors():
    class C:
        def __init__(self, r):
            self.r = r

        async def watch_beat(self, *a, **kw):
            if isinstance(self.r, Exception):
                raise self.r
            return self.r

    st = _WatchClaimState("s")
    st.held = True
    assert await st.beat(C({"verdict": "holder"})) == "holder"
    assert await st.beat(C({"verdict": "displaced"})) == "displaced"
    # lost response is UNKNOWN — pause, not death, not emission
    assert await st.beat(C(ConnectionError("blip"))) == "unknown"


def _stream_probe(cmd: str, fifo: str, timeout: float = 3.0) -> str:
    """Run `cmd` (the hinted consumer) against `fifo` while a writer HOLDS the
    FIFO open, write two lines, and return what the consumer emitted within
    `timeout`. A consumer that only prints at writer-EOF returns ''."""
    import subprocess
    import time
    proc = subprocess.Popen(
        ["sh", "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        w = open(fifo, "w", buffering=1)       # blocks until the consumer attaches
        try:
            w.write("wake-1\n"); w.write("wake-2\n")
            got = b""
            deadline = time.monotonic() + timeout
            os.set_blocking(proc.stdout.fileno(), False)
            while time.monotonic() < deadline and b"wake-2" not in got:
                try:
                    chunk = os.read(proc.stdout.fileno(), 4096)
                    if chunk:
                        got += chunk
                        continue
                except BlockingIOError:
                    pass
                time.sleep(0.05)
            return got.decode()
        finally:
            w.close()                           # writer EOF — AFTER the read window
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_watcher_attach_command_streams_live(tmp_path, monkeypatch):
    """The command memory_status hands the agent must emit each wake AS IT
    LANDS while the watcher (the writer) is alive. `tail -F` does not: on a
    FIFO it reads to EOF before printing, EOF never comes while the watcher
    lives, so the seat read `covered` and the session was deaf
    (owner-found, 2026-08-21). This runs the hinted command for real."""
    fifo = str(tmp_path / "wake.fifo")
    os.mkfifo(fifo, 0o600)
    monkeypatch.setitem(server._WATCHER_SUP, "fifo", fifo)
    cmd = server._watcher_attach_command()
    assert cmd and fifo in cmd
    got = _stream_probe(cmd, fifo)
    assert "wake-1" in got and "wake-2" in got, (
        f"hinted consumer {cmd!r} emitted {got!r} while the writer was alive — "
        "a covered seat whose wakes never reach the session")


def test_tail_on_a_fifo_is_deaf_until_writer_eof(tmp_path):
    """The negative control: this is WHY the hint is not tail. If a platform
    ever makes tail stream a live FIFO, this goes red and the comment in
    _watcher_attach_command can be revisited — until then it documents the
    measured failure."""
    fifo = str(tmp_path / "wake.fifo")
    os.mkfifo(fifo, 0o600)
    got = _stream_probe(f"tail -F {fifo}", fifo, timeout=1.5)
    assert got == "", f"tail -F streamed a live FIFO here: {got!r}"


@pytest.mark.asyncio
async def test_release_on_exit_gives_the_watch_back_once():
    """WATCH-CLAIM-4(c), wild specimen 2026-08-21: a watcher that died with
    the session (restart) left its claim held; the successor sat `held` until
    expiry. release() must POST once, flip held, clear the active record, and
    be idempotent; a not-held state must not POST at all."""
    import engram_mcp.inbox_wait as iw
    released = []

    class FakeClient:
        async def watch_claim(self, **kw):
            return {"verdict": "granted"}

        async def watch_release(self, seat, nonce):
            released.append((seat, nonce))
            return {"verdict": "released"}

    st = _WatchClaimState("proj-claude-2")
    assert await st.acquire(FakeClient(), "/tmp", ["proj-claude-2"]) is None
    assert st.held and iw._ACTIVE_CLAIM is st

    assert await st.release(FakeClient()) is True
    assert released == [("proj-claude-2", st.nonce)]
    assert not st.held and iw._ACTIVE_CLAIM is None
    assert await st.release(FakeClient()) is False and len(released) == 1

    # not-held (never granted / unheld mode) never POSTs
    st2 = _WatchClaimState("proj-claude-3")
    assert await st2.release(FakeClient()) is False and len(released) == 1


def test_release_after_signal_uses_a_fresh_loop(monkeypatch):
    """The SIGTERM path runs after asyncio.run() has torn the loop down —
    _release_after_signal must still POST the release (fresh loop), and be a
    no-op when nothing is held."""
    import engram_mcp.inbox_wait as iw
    released = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def watch_release(self, seat, nonce):
            released.append((seat, nonce))
            return {"verdict": "released"}

        async def close(self):
            pass

    monkeypatch.setattr(iw, "MemoryClient", FakeClient)
    st = _WatchClaimState("proj-claude-9")
    st.held = True
    monkeypatch.setattr(iw, "_ACTIVE_CLAIM", st)
    iw._release_after_signal()
    assert released == [("proj-claude-9", st.nonce)] and not st.held
    iw._release_after_signal()          # idempotent, nothing held now
    assert len(released) == 1



@pytest.mark.asyncio
async def test_pause_then_recover_logs_both_once(capsys):
    """OBS (2026-08-21): a lost beat logs a pause; the recovery on the next
    successful beat must ALSO log, once — otherwise a log ending at the pause
    line reads as a stuck watcher when it has in fact resumed and gone quiet.
    """
    class C:
        def __init__(self, r):
            self.r = r

        async def watch_beat(self, *a, **kw):
            if isinstance(self.r, Exception):
                raise self.r
            return self.r

    st = _WatchClaimState("s")
    st.held = True

    assert await st.beat(C(ConnectionError("blip"))) == "unknown"
    assert await st.beat(C(ConnectionError("blip"))) == "unknown"  # still down
    assert await st.beat(C({"verdict": "holder"})) == "holder"     # recovered
    err = capsys.readouterr().err
    # pause logged exactly once across the outage (not once per failed cycle)
    assert err.count("pausing emission until a verdict") == 1
    # recovery logged exactly once
    assert err.count("watch beat recovered — emission resumed") == 1


@pytest.mark.asyncio
async def test_steady_holder_beats_log_nothing(capsys):
    class C:
        async def watch_beat(self, *a, **kw):
            return {"verdict": "holder"}

    st = _WatchClaimState("s")
    st.held = True
    for _ in range(3):
        assert await st.beat(C()) == "holder"
    err = capsys.readouterr().err
    assert "pausing emission" not in err
    assert "emission resumed" not in err  # no false "recovered" without a pause
