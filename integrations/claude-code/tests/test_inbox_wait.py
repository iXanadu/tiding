"""Tests for engram-inbox-wait, the shell-callable inbox watcher."""

import json

import httpx
import respx

from engram_mcp.client import MemoryClient
from engram_mcp.inbox_wait import (
    _emit,
    _emit_backlog_digest,
    _is_immortal_address,
    _poll,
    _run,
)


def _msg(i, **kw):
    d = {
        "id": f"inbox/{i}",
        "from": "peer@elsewhere",
        "subject": f"s{i}",
        "thread_id": None,
        "created_at": "2026-06-14T00:00:00Z",
    }
    d.update(kw)
    return d


class _Args:
    def __init__(self, **kw):
        self.project_dir = ""
        self.address = "engram"
        self.poll_interval = 0.0
        self.follow = False
        self.timeout = 5.0
        self.include_existing = False
        self.__dict__.update(kw)


def test_emit_is_one_json_line(capsys):
    _emit(_msg(1, **{"from": "agentbeast"}))
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj == {
        "id": "inbox/1",
        "from": "agentbeast",
        "subject": "s1",
        "thread_id": None,
        "created_at": "2026-06-14T00:00:00Z",
    }


def test_emit_handles_from_underscore_alias(capsys):
    _emit({"id": "inbox/9", "from_": "engram", "subject": "x"})
    obj = json.loads(capsys.readouterr().out.strip())
    assert obj["from"] == "engram"


@respx.mock(base_url="http://localhost:8920")
async def test_poll_returns_fresh_and_dedups(respx_mock):
    respx_mock.post("/memory/inbox").mock(
        return_value=httpx.Response(200, json={"status": "ok", "messages": [_msg(1), _msg(2)]})
    )
    c = MemoryClient("http://localhost:8920", "")
    seen: set = set()
    fresh = await _poll(c, ["engram"], "engram@h", seen)
    assert {m["id"] for m in fresh} == {"inbox/1", "inbox/2"}
    # same backlog on the next poll → nothing new
    assert await _poll(c, ["engram"], "engram@h", seen) == []
    await c.close()


@respx.mock(base_url="http://localhost:8920")
async def test_poll_requests_newest_first(respx_mock):
    # Load-bearing: the watcher never acks, so an oldest-first LIMIT would
    # truncate new mail out of the window once the backlog exceeds it. The
    # watcher MUST request newest_first so new arrivals stay in-window.
    route = respx_mock.post("/memory/inbox").mock(
        return_value=httpx.Response(200, json={"status": "ok", "messages": []})
    )
    c = MemoryClient("http://localhost:8920", "")
    await _poll(c, ["engram"], "engram@h", set())
    body = json.loads(route.calls.last.request.content)
    assert body["newest_first"] is True
    await c.close()


@respx.mock(base_url="http://localhost:8920")
async def test_poll_skips_self_echo(respx_mock):
    # reader is beastchat@macmini; its own sends carry from=beastchat@macmini
    # (full form) — and its loose name "beastchat" is equally self. Both must be
    # dropped; mail from a real peer must survive.
    respx_mock.post("/memory/inbox").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "messages": [
                    _msg(1, **{"from": "beastchat@macmini"}),  # own outbound, full form
                    _msg(2, **{"from": "beastchat"}),          # own outbound, loose form
                    _msg(3, **{"from": "engram@macmini"}),     # genuine peer
                ],
            },
        )
    )
    c = MemoryClient("http://localhost:8920", "")
    seen: set = set()
    fresh = await _poll(c, ["beastchat"], "beastchat@macmini", seen)
    assert {m["id"] for m in fresh} == {"inbox/3"}
    # self-echoes were still recorded as seen, so they never re-fire either
    assert "inbox/1" in seen and "inbox/2" in seen
    await c.close()


@respx.mock(base_url="http://localhost:8920")
async def test_poll_sibling_survives_under_distinct_identities(respx_mock):
    # With a per-session identity (beastchat-app), the sibling session
    # (beastchat-server) is NOT self — its mail must wake us; only our own
    # outbound is dropped.
    respx_mock.post("/memory/inbox").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "messages": [
                    _msg(1, **{"from": "beastchat-app@macmini"}),     # own echo → drop
                    _msg(2, **{"from": "beastchat-server@macmini"}),  # sibling → keep
                ],
            },
        )
    )
    c = MemoryClient("http://localhost:8920", "")
    fresh = await _poll(c, ["beastchat-app", "beastchat"], "beastchat-app@macmini", set())
    assert {m["id"] for m in fresh} == {"inbox/2"}
    await c.close()


@respx.mock(base_url="http://localhost:8920")
async def test_poll_raises_on_bad_status(respx_mock):
    respx_mock.post("/memory/inbox").mock(
        return_value=httpx.Response(200, json={"status": "error"})
    )
    c = MemoryClient("http://localhost:8920", "")
    try:
        raised = False
        try:
            await _poll(c, ["engram"], "engram@h", set())
        except RuntimeError:
            raised = True
        assert raised
    finally:
        await c.close()


@respx.mock(base_url="http://localhost:8920")
async def test_run_oneshot_exits_on_new_mail(respx_mock, capsys):
    respx_mock.post("/memory/inbox").mock(
        return_value=httpx.Response(200, json={"status": "ok", "messages": [_msg(1)]})
    )
    rc = await _run(_Args(include_existing=True))
    assert rc == 0
    assert json.loads(capsys.readouterr().out.strip())["id"] == "inbox/1"


@respx.mock(base_url="http://localhost:8920")
async def test_run_seeds_backlog_then_wakes_only_on_new(respx_mock, capsys):
    route = respx_mock.post("/memory/inbox")
    route.side_effect = [
        httpx.Response(200, json={"status": "ok", "messages": [_msg(1)]}),            # seed: backlog
        httpx.Response(200, json={"status": "ok", "messages": [_msg(1)]}),            # poll 1: nothing new
        httpx.Response(200, json={"status": "ok", "messages": [_msg(1), _msg(2)]}),   # poll 2: NEW inbox/2
    ]
    rc = await _run(_Args(include_existing=False))
    assert rc == 0
    # only the message that arrived AFTER start, never the seeded backlog
    out = capsys.readouterr().out.strip()
    assert json.loads(out)["id"] == "inbox/2"


# --- LANE-3: arm-time backlog digest over immortal addresses ---------------
# Subsumes MSG-7's queued-directives line: ONE arm-time surface, never two.
# Wake path stays wake-on-new-only (created_at after ARM time, the seed
# boundary). The unread state is the discriminator, per-reader, as today.

def test_digest_is_one_line_counts_only_immortal(capsys):
    """Mixed backlog → exactly one digest line (counts + senders, no bodies),
    covering lane / project / group / #channel; seat-DMs and machine: never
    enter it (seat-pinned threads die with their occupant — settled)."""
    backlog = [
        _msg(1, to="proj", intent="action"),
        _msg(2, to="proj-claude"),
        _msg(3, to="proj-claude", **{"from": "other@box"}),
        _msg(4, to="proj-app", intent="fyi"),
        _msg(5, to="#devagents"),
        _msg(6, to="proj-claude-2"),          # occupant seat-DM → excluded
        _msg(7, to="machine:macmini"),        # machine → excluded
    ]
    n = _emit_backlog_digest(backlog, occupant="proj-claude-2", project="proj")
    assert n == 5
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1, "one digest line, never a firehose"
    obj = json.loads(lines[0])
    assert obj["event"] == "backlog-digest"
    assert set(obj["addresses"]) == {"proj", "proj-claude", "proj-app",
                                     "#devagents"}
    assert obj["addresses"]["proj-claude"]["unread_count"] == 2
    assert obj["directive_count"] == 1          # action only; fyi never
    assert "body" not in json.dumps(obj)


def test_105_messages_are_one_line_with_a_count(capsys):
    backlog = [_msg(i, to="softphone-claude") for i in range(105)]
    assert _emit_backlog_digest(backlog, occupant="softphone-claude-2",
                                project="softphone") == 105
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["addresses"]["softphone-claude"]["unread_count"] == 105


def test_authority_directive_wakes_individually_age_ignored(capsys):
    """authority-directive ONLY gets the individual age-proof wake; several
    collapse to ONE event carrying the newest + a count. Digest and the
    authority event are at most two lines total — never queued-directives."""
    backlog = [
        _msg(1, to="proj", intent="authority-directive",
             created_at="2026-07-01T00:00:00Z"),
        _msg(2, to="proj", intent="authority-directive",
             created_at="2026-08-01T00:00:00Z"),
        _msg(3, to="proj", intent="action"),
    ]
    _emit_backlog_digest(backlog, occupant="proj-claude", project="proj")
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2
    events = {json.loads(l)["event"] for l in lines}
    assert events == {"backlog-digest", "authority-directive-queued"}
    auth = next(json.loads(l) for l in lines
                if json.loads(l)["event"] == "authority-directive-queued")
    assert auth["count"] == 2
    assert auth["id"] == "inbox/2", "newest wins the detail slot"
    assert "body" not in auth


def test_digest_silent_when_backlog_empty_or_all_seat_dms(capsys):
    assert _emit_backlog_digest([], "proj-claude", "proj") == 0
    assert _emit_backlog_digest(
        [_msg(1, to="proj-claude-2")], occupant="proj-claude-2",
        project="proj") == 0
    assert capsys.readouterr().out == ""


def test_unseated_session_keeps_its_project_channel_in_the_digest(capsys):
    """Unseated: the reader name IS the project — a channel, not a seat.
    The occupant exclusion must not eat it."""
    assert _emit_backlog_digest(
        [_msg(1, to="proj")], occupant="proj", project="proj") == 1
    assert json.loads(capsys.readouterr().out)["addresses"]["proj"][
        "unread_count"] == 1


def test_is_immortal_address_edges():
    assert _is_immortal_address("proj@host", "proj-claude", "proj")
    assert not _is_immortal_address("", "x", "y")
    assert not _is_immortal_address("machine:host", "x", "y")
    assert not _is_immortal_address("proj-claude@host", "proj-claude", "proj")
    assert _is_immortal_address("proj@host", "proj", "proj")


@respx.mock(base_url="http://localhost:8920")
async def test_oneshot_wakes_immediately_on_queued_backlog(respx_mock, capsys):
    """One-shot mode: a digest-worthy backlog at start IS the wake — the
    watcher must not sit waiting for the NEXT message while unread immortal
    mail sits in the backlog it just seeded past."""
    respx_mock.post("/memory/inbox").mock(
        return_value=httpx.Response(200, json={"status": "ok", "messages": [
            _msg(1, to="engram", intent="action")]})
    )
    rc = await _run(_Args(include_existing=False))
    assert rc == 0
    obj = json.loads(capsys.readouterr().out.strip())
    assert obj["event"] == "backlog-digest"
    assert obj["addresses"]["engram"]["unread_count"] == 1


@respx.mock(base_url="http://localhost:8920")
async def test_follow_emits_summary_then_new_mail(respx_mock, capsys):
    """Follow mode: the summary lands first, then the watcher keeps watching —
    the seeded directive is never re-emitted individually."""
    route = respx_mock.post("/memory/inbox")
    route.side_effect = [
        httpx.Response(200, json={"status": "ok", "messages": [
            _msg(1, to="engram", intent="action")]}),                 # seed
        httpx.Response(200, json={"status": "ok", "messages": [
            _msg(1, to="engram", intent="action"), _msg(2)]}),        # poll: NEW inbox/2
    ]
    rc = await _run(_Args(include_existing=False, follow=True, timeout=0.001))
    assert rc == 0
    lines = [json.loads(l) for l in capsys.readouterr().out.strip().splitlines()]
    assert lines[0]["event"] == "backlog-digest"
    assert lines[1]["id"] == "inbox/2"
    assert len(lines) == 2, "the seeded backlog must not fire twice"


@respx.mock(base_url="http://localhost:8920")
async def test_poll_fyi_intent_never_wakes(respx_mock):
    """MSG-3 wake-gating: intent='fyi' is recorded as seen but never emitted
    (no wake). action / authority-directive / missing intent all wake."""
    respx_mock.post("/memory/inbox").mock(
        return_value=httpx.Response(200, json={"status": "ok", "messages": [
            _msg(1, intent="fyi"),
            _msg(2, intent="action"),
            _msg(3, intent="authority-directive"),
            _msg(4),  # no intent (legacy) → wakes
        ]})
    )
    c = MemoryClient("http://localhost:8920", "")
    seen: set = set()
    fresh = await _poll(c, ["engram"], "engram@h", seen)
    assert {m["id"] for m in fresh} == {"inbox/2", "inbox/3", "inbox/4"}
    # the fyi is seen (won't re-wake later) even though it never emitted
    assert "inbox/1" in seen
    await c.close()


# --- 2026-07-21 audit: fail LOUD on auth rejection, warn on plaintext ---

@respx.mock(base_url="http://localhost:8920")
async def test_run_exits_on_401_instead_of_retrying(respx_mock, capsys, monkeypatch):
    """An auth rejection is not transient — retry-forever means every wake is
    silently missed. The watcher must die with EXIT_AUTH_FAILED."""
    from engram_mcp import inbox_wait as iw

    respx_mock.post("/memory/inbox").mock(
        return_value=httpx.Response(401, json={"detail": "Invalid API token."})
    )
    # include_existing so the seed poll is skipped and the loop hits the 401
    rc = await _run(_Args(include_existing=True, follow=True, timeout=5.0))
    assert rc == iw.EXIT_AUTH_FAILED
    err = capsys.readouterr().err
    assert "FATAL" in err and "~/.config/engram/identity" in err


@respx.mock(base_url="http://localhost:8920")
async def test_run_exits_on_403(respx_mock, capsys):
    from engram_mcp import inbox_wait as iw

    respx_mock.post("/memory/inbox").mock(
        return_value=httpx.Response(403, json={"detail": "forbidden"})
    )
    rc = await _run(_Args(include_existing=True, follow=True, timeout=5.0))
    assert rc == iw.EXIT_AUTH_FAILED


@respx.mock(base_url="http://localhost:8920")
async def test_run_keeps_polling_on_transient_500(respx_mock, capsys):
    """Non-auth server blips must NOT kill a long-lived watcher (unchanged)."""
    calls = {"n": 0}

    def _responder(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, json={"detail": "boom"})
        return httpx.Response(200, json={"status": "ok", "messages": [_msg(1)]})

    respx_mock.post("/memory/inbox").mock(side_effect=_responder)
    rc = await _run(_Args(include_existing=True))
    assert rc == 0  # survived the 500, woke on the mail after
    assert "retrying" in capsys.readouterr().err


def test_plaintext_warning_for_remote_http(capsys):
    from engram_mcp.inbox_wait import _warn_plaintext_url

    _warn_plaintext_url("http://macmini:8920")
    assert "unencrypted" in capsys.readouterr().err


def test_no_plaintext_warning_for_localhost_or_https(capsys):
    from engram_mcp.inbox_wait import _warn_plaintext_url

    _warn_plaintext_url("http://localhost:8920")
    _warn_plaintext_url("http://127.0.0.1:8920")
    _warn_plaintext_url("https://engram.example.com")
    assert capsys.readouterr().err == ""


# --- MSG-5: the watcher reports that an EAR is alive -----------------------


@respx.mock(base_url="http://localhost:8920")
async def test_watcher_beats_presence_each_poll(respx_mock, capsys):
    """The watcher is the only process whose presence means mail wakes anyone.

    Before this it polled silently, so engram had no way to tell a listening
    session from a permanently deaf one — and the roster reported both as
    running.
    """
    beat = respx_mock.post("/memory/presence").mock(
        return_value=httpx.Response(200, json={"status": "ok", "identity": "engram",
                                               "state": "running", "collision": None})
    )
    respx_mock.post("/memory/inbox").mock(
        return_value=httpx.Response(200, json={"status": "ok", "messages": [_msg(1)]})
    )
    rc = await _run(_Args(include_existing=True))
    assert rc == 0
    assert beat.called
    sent = json.loads(beat.calls[0].request.content)
    assert sent["watcher"] is True
    # Carries no session state and no nonce: it must neither overwrite what the
    # session reported nor read as a second live session on the same identity.
    assert sent.get("session_nonce") is None


@respx.mock(base_url="http://localhost:8920")
async def test_beat_failure_never_stops_a_wake(respx_mock, capsys):
    """Bookkeeping must not gate delivery.

    If reporting "I am listening" could break listening, the feature would
    cost more than it buys.
    """
    respx_mock.post("/memory/presence").mock(return_value=httpx.Response(500, json={}))
    respx_mock.post("/memory/inbox").mock(
        return_value=httpx.Response(200, json={"status": "ok", "messages": [_msg(7)]})
    )
    rc = await _run(_Args(include_existing=True))
    assert rc == 0
    assert json.loads(capsys.readouterr().out.strip())["id"] == "inbox/7"


# --- WATCH-1: refuse an inherited identity that contradicts --project-dir ---


from engram_mcp import identity as _identity
from engram_mcp.inbox_wait import (
    EXIT_IDENTITY_CONFLICT,
    _identity_conflict,
)


def test_conflict_predicate_project_and_derived_seats_are_coherent():
    """The project's own name and any seat derived from it pass — this is
    what keeps the documented co-working form (ENGRAM_INBOX_IDENTITY=
    <project>-<role>) working unchanged."""
    assert _identity_conflict("admin@box", "admin", False) is None
    assert _identity_conflict("engram-claude-2@box", "engram", False) is None
    assert _identity_conflict("engram-audit@box", "engram", False) is None


def test_conflict_predicate_cross_folder_identity_is_refused():
    """The measured hijack: a watcher for ~/maintenance (project=admin)
    inheriting 'maintenance-grok' from a daemon's frozen env. Four incidents,
    all silent; now a named refusal."""
    msg = _identity_conflict("maintenance-grok@box", "admin", False)
    assert msg is not None
    assert "maintenance-grok" in msg and "admin" in msg
    assert "--identity" in msg, "the refusal must name the escape hatch"


def test_conflict_predicate_explicit_assertion_always_wins():
    """Stating an intent differs from leaking one."""
    assert _identity_conflict("maintenance-grok@box", "admin", True) is None


async def test_run_refuses_inherited_conflicting_identity(
    tmp_path, monkeypatch, capsys
):
    """Arm-time refusal, before any network I/O: exit EXIT_IDENTITY_CONFLICT
    with a message naming both identities."""
    proj = tmp_path / "alpha"
    proj.mkdir()
    (proj / ".engram.cfg").write_text("project = alpha\n")
    monkeypatch.setenv("ENGRAM_INBOX_IDENTITY", "otherproj-grok")
    _identity.clear_seat()
    prev_anchor = _identity._IDENTITY_ANCHOR

    try:
        rc = await _run(_Args(project_dir=str(proj), address=""))
        assert rc == EXIT_IDENTITY_CONFLICT
        err = capsys.readouterr().err
        assert "otherproj-grok" in err and "alpha" in err
    finally:
        _identity._IDENTITY_ANCHOR = prev_anchor
        _identity.reset_session_pin()


@respx.mock(base_url="http://localhost:8920")
async def test_run_identity_flag_asserts_and_proceeds(
    respx_mock, tmp_path, monkeypatch, capsys
):
    """--identity is the explicit channel: same poisoned env, but the
    operator asserts the seat — the watcher starts, listens as asserted, and
    does NOT write the session's shared seat file (a watcher re-seating the
    whole session would be the hijack pointed the other way)."""
    proj = tmp_path / "alpha"
    proj.mkdir()
    (proj / ".engram.cfg").write_text("project = alpha\n")
    seats = tmp_path / "seats"
    monkeypatch.setenv("ENGRAM_INBOX_IDENTITY", "otherproj-grok")
    monkeypatch.setenv("ENGRAM_SEATS_DIR", str(seats))
    _identity.clear_seat()
    prev_anchor = _identity._IDENTITY_ANCHOR

    respx_mock.post("/memory/inbox").mock(
        return_value=httpx.Response(200, json={"status": "ok", "messages": []})
    )
    respx_mock.post("/memory/presence").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    rc = await _run(_Args(
        project_dir=str(proj), address="", identity="otherproj-grok",
        timeout=0.001, follow=True, include_existing=True,
    ))
    try:
        assert rc == 0
        assert not seats.exists() or not any(seats.iterdir()), (
            "--identity must not write the session's shared seat file"
        )
    finally:
        _identity.clear_seat()
        _identity._IDENTITY_ANCHOR = prev_anchor
        _identity.reset_session_pin()
