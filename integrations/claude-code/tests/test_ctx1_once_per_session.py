"""CTX-1 (2026-08-21): reference text is shown in full ONCE per session, then
as a one-liner. A Claude session measured ~575 tokens for memory_inbox to say
"empty" and ~1.5k chars of identical ⛔ banner on every tool result during
startup. The instruction never changes — only the repetition goes — and the
per-call FACTS (digest counts, estate warnings, listen_set, attach command)
are always kept."""
from engram_mcp import server as srv


def _reset():
    srv._SHOWN_ONCE.clear()


def test_wake_banner_full_once_then_short_then_full_again_after_recovery(monkeypatch):
    _reset()
    monkeypatch.setitem(srv._WATCHER_SUP, "fifo", "/tmp/x.fifo")
    monkeypatch.setattr(srv, "resolve_provider", lambda: "claude")
    monkeypatch.setitem(srv._WATCH_STATE, "state", "expired")
    monkeypatch.setitem(srv._WATCH_STATE, "seat", "proj-claude-2")
    first = srv._wake_stream_banner()
    assert "Never launch engram-inbox-wait" in first          # full form
    second = srv._wake_stream_banner()
    assert second.startswith("⛔ WAKE STREAM NOT COVERED")
    assert "while true; do cat /tmp/x.fifo" in second        # the command is a FACT — always kept
    assert "Never launch engram-inbox-wait" not in second     # the essay is not
    assert len(second) < len(first) / 2
    # coverage returns, then is lost again → a NEW episode is full again
    monkeypatch.setitem(srv._WATCH_STATE, "state", "covered")
    assert srv._wake_stream_banner() == ""
    monkeypatch.setitem(srv._WATCH_STATE, "state", "unheld")
    assert "Never launch engram-inbox-wait" in srv._wake_stream_banner()


def test_collision_banner_full_once_then_short(monkeypatch):
    _reset()
    monkeypatch.setattr(srv, "compute_identity", lambda pd=None: ("proj-claude-4@box", ["proj-claude-4"]))
    monkeypatch.setattr(srv, "_SEAT_COLLISION", {"live_sessions": 2, "providers": ["claude"]})
    first = srv._seat_collision_banner()
    assert "predecessor's dying tail" in first
    second = srv._seat_collision_banner()
    assert second.startswith("⛔ SEAT COLLISION")
    assert "memory_take_seat(name='proj-claude-4-<role>'" in second   # the fix stays
    assert "predecessor's dying tail" not in second
    monkeypatch.setattr(srv, "_SEAT_COLLISION", None)
    assert srv._seat_collision_banner() == ""
    monkeypatch.setattr(srv, "_SEAT_COLLISION", {"live_sessions": 2, "providers": ["claude"]})
    assert "predecessor's dying tail" in srv._seat_collision_banner()


_EMPTY_INBOX_GUIDANCE = (
    "📬 6 open · 158 resolved/superseded hidden.\n"
    "No open messages right now. Polling cadence:\n"
    "  • memory_search automatically shows a 📬 INBOX banner when there is\n"
    "    unread mail — you do not need to poll memory_inbox on a timer.\n"
    "  • An empty inbox is NOT an empty room. Huddles run letters-off:\n"
    "    a room records every utterance in its TRANSCRIPT.\n"
    "  • ⚠️  6 message(s) are OPEN on your addresses and NOT\n"
    "    shown here — already acked, but by a PREDECESSOR.\n"
    "  • You are listening as 'p@box' on: ['p', 'p@box']"
)


def test_guidance_full_once_then_facts_only():
    _reset()
    full = srv._compact_guidance(_EMPTY_INBOX_GUIDANCE)
    assert full == _EMPTY_INBOX_GUIDANCE
    short = srv._compact_guidance(_EMPTY_INBOX_GUIDANCE)
    assert "📬 6 open" in short                                  # digest kept
    assert "6 message(s) are OPEN on your addresses" in short     # estate warning kept
    assert "already acked, but by a PREDECESSOR" in short          # ...with its continuation
    assert "You are listening as 'p@box'" in short                # listen_set kept
    assert "Huddles run letters-off" not in short                 # essay dropped
    assert "shown in full earlier this session" in short
    # a DIFFERENT kind is still full the first time it appears
    other = "Handling messages:\n  • Reply: memory_reply(...)\n  • You are listening as 'p@box' on: ['p']."
    assert srv._compact_guidance(other) == other
    assert "memory_reply" not in srv._compact_guidance(other)


def test_unknown_guidance_kind_is_never_compacted():
    _reset()
    g = "Something new the server says.\n  • with a bullet"
    assert srv._compact_guidance(g) == g
    assert srv._compact_guidance(g) == g
