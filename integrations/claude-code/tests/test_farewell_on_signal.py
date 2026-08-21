"""FAREWELL-ON-SIGNAL: the bridge-owned watcher is SIGTERMed by the bridge's
atexit on the commonest session exit, so the in-loop "watcher outlives the
session and farewells" path never ran there. The signal path must OBSERVE the
session before dying: gone → farewell (no gasp — nobody left to tell); alive
→ the gasp as before; no session identified → never a farewell.

Measured 2026-08-21: the owner restarted both sessions, walked the dog an
hour, and peers were still routing around his dead seats — every one of
them read `running` with farewell_at=None for the 48h retention window.
"""
import engram_mcp.inbox_wait as iw


class _FakeClient:
    calls: list = []

    def __init__(self, *a, **k):
        pass

    async def presence_farewell(self, identity, project, project_dir=None):
        _FakeClient.calls.append((identity, project))
        return {"ok": True}

    async def close(self):
        pass


def _arm(monkeypatch, watched, ident="proj-claude-9", gone_seq=None):
    _FakeClient.calls = []
    monkeypatch.setattr(iw, "MemoryClient", _FakeClient)
    monkeypatch.setattr(iw, "_WATCHED", watched)
    monkeypatch.setattr(iw, "_WATCHED_READER_IDENTITY", ident)
    monkeypatch.setattr(iw, "_WATCHED_PROJECT_DIR", "/tmp/proj")
    monkeypatch.setattr(iw, "derive_project_name", lambda d: "proj")
    seq = list(gone_seq or [])

    def fake_gone(pid, start):
        return seq.pop(0) if seq else False
    monkeypatch.setattr(iw, "process_is_gone", fake_gone)


def test_session_gone_sends_farewell_and_reports_it(monkeypatch, capsys):
    _arm(monkeypatch, (4242, "Fri Aug 21 11:22:59 2026"),
         gone_seq=[True, True])
    assert iw._farewell_after_signal(window=2.0, poll=0.01) is True
    assert _FakeClient.calls == [("proj-claude-9", "proj")]
    err = capsys.readouterr().err
    assert "farewell recorded" in err


def test_one_positive_is_not_enough(monkeypatch):
    """Two consecutive positives, same as the in-loop path — a false farewell
    is the expensive direction."""
    _arm(monkeypatch, (4242, "x"), gone_seq=[True, False, True, False])
    assert iw._farewell_after_signal(window=0.1, poll=0.01) is False
    assert _FakeClient.calls == []


def test_session_still_alive_at_deadline_means_no_farewell(monkeypatch):
    _arm(monkeypatch, (4242, "x"), gone_seq=[False] * 50)
    assert iw._farewell_after_signal(window=0.05, poll=0.01) is False
    assert _FakeClient.calls == []


def test_no_session_identified_never_farewells(monkeypatch):
    _arm(monkeypatch, None, gone_seq=[True, True])
    assert iw._farewell_after_signal(window=0.05, poll=0.01) is False
    assert _FakeClient.calls == []


def test_sigterm_path_skips_the_gasp_when_the_session_died(monkeypatch, capsys):
    """main()'s SIGTERM branch: farewell sent → no 'possibly alive' gasp."""
    _arm(monkeypatch, (4242, "x"), gone_seq=[True, True])
    monkeypatch.setattr(iw, "FAREWELL_ON_SIGNAL_WINDOW", 1.0)
    monkeypatch.setattr(iw, "FAREWELL_ON_SIGNAL_POLL", 0.01)
    gasps = []
    monkeypatch.setattr(iw, "_dying_gasp", lambda reason: gasps.append(reason))
    monkeypatch.setattr(iw, "_release_after_signal", lambda: None)

    async def _boom(args):
        raise SystemExit(143)
    monkeypatch.setattr(iw, "_run", _boom)
    monkeypatch.setattr(iw.sys, "argv", ["engram-inbox-wait", "--follow"])
    try:
        iw.main()
    except SystemExit as e:
        assert e.code == 143
    assert gasps == []
    assert _FakeClient.calls == [("proj-claude-9", "proj")]

    # And the alive case still gasps.
    _arm(monkeypatch, (4242, "x"), gone_seq=[False] * 50)
    monkeypatch.setattr(iw, "FAREWELL_ON_SIGNAL_WINDOW", 0.05)
    try:
        iw.main()
    except SystemExit:
        pass
    assert gasps and "SIGTERM" in gasps[0]
