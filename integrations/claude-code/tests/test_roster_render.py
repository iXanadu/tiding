"""memory_roster rendering — the four watcher states, as OBSERVATIONS.

ROSTER-FAREWELL-RENDER (2026-08-21): the server has returned `farewell_at`
on roster entries since FAREWELL-1 (ed5709b), but the bridge renderer only
knew three beat states, so a seat whose watcher OBSERVED the session exit
rendered as "watcher gone quiet" — indistinguishable from a busy session.
Measured on two seats across two projects the morning after it shipped.
These tests pin all four branches, because none of them had a test before.
"""

import pytest

from engram_mcp import server as srv


def _entry(identity, **kw):
    base = {
        "identity": identity,
        "project": "proj",
        "provider": "claude",
        "age_seconds": 12.0,
        "is_stale": False,
        "collision": False,
        "watcher_alive": None,
        "farewell_at": None,
    }
    base.update(kw)
    return base


async def _render(monkeypatch, entries):
    async def _noop_heartbeat(project_dir):
        return None

    async def _fake_roster(**kw):
        return {"status": "ok", "entries": entries}

    monkeypatch.setattr(srv, "_heartbeat", _noop_heartbeat)
    monkeypatch.setattr(srv._client, "roster", _fake_roster)
    return await srv.memory_roster(project="proj", project_dir="/tmp/x")


@pytest.mark.asyncio
async def test_three_beat_states_render_as_observations(monkeypatch):
    out = await _render(monkeypatch, [
        _entry("proj-claude-1", watcher_alive=True),
        _entry("proj-claude-2", watcher_alive=False),
        _entry("proj-claude-3", watcher_alive=None),
    ])
    assert "proj-claude-1" in out and "watcher beat recently" in out
    assert "proj-claude-2" in out and "watcher gone quiet" in out
    assert "proj-claude-3" in out and "no watcher seen" in out
    # a fresh-but-quiet seat gets the "no watcher beat" advisory; it is NOT
    # called dead (a busy agent and a dead one are both silent — MSG-8).
    assert "ADDRESSABLE, NO WATCHER BEAT: proj-claude-2" in out
    assert "EXITED" not in out


@pytest.mark.asyncio
async def test_farewell_renders_as_observed_exit_not_silence(monkeypatch):
    out = await _render(monkeypatch, [
        _entry("proj-claude-5", watcher_alive=False, is_stale=True,
               farewell_at="2026-08-21T15:53:07.123456Z"),
        _entry("proj-claude-6", watcher_alive=True),
    ])
    line = next(l for l in out.splitlines() if l.strip().startswith("proj-claude-5"))
    # the observation, with the time, on the seat's own line
    assert "watcher OBSERVED the session exit at 2026-08-21T15:53:07Z" in line
    # it OUTRANKS the beat state: not "gone quiet" on that line
    assert "watcher gone quiet" not in line
    # and the seat is called out in its own footer, as an exit, not as silence
    assert "☠ EXITED (observed): proj-claude-5" in out
    assert "Do not hand work to these chairs" in out
    # the live seat is untouched
    assert "proj-claude-6" in out and "watcher beat recently" in out


@pytest.mark.asyncio
async def test_farewelled_seat_never_lands_in_no_watcher_beat_advisory(monkeypatch):
    # The advisory text says "a session can be doing real work with a dead
    # watcher" — the one thing an OBSERVED exit rules out. A farewelled seat
    # that is not yet stale must not be listed there.
    out = await _render(monkeypatch, [
        _entry("proj-claude-7", watcher_alive=False, is_stale=False,
               farewell_at="2026-08-21T16:00:00Z"),
    ])
    assert "☠ EXITED (observed): proj-claude-7" in out
    assert "ADDRESSABLE, NO WATCHER BEAT" not in out


def test_short_ts_trims_iso_and_passes_odd_shapes_through():
    assert srv._short_ts("2026-08-21T15:53:07.123456+00:00") == "2026-08-21T15:53:07Z"
    assert srv._short_ts("2026-08-21T15:53:07Z") == "2026-08-21T15:53:07Z"
    # never hide the fact because of its shape
    assert srv._short_ts("yesterday-ish") == "yesterday-ish"
    assert srv._short_ts(1755790000) == "1755790000"
