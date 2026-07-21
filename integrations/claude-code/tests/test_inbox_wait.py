"""Tests for engram-inbox-wait, the shell-callable inbox watcher."""

import json

import httpx
import respx

from engram_mcp.client import MemoryClient
from engram_mcp.inbox_wait import _emit, _poll, _run


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
