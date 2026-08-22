"""Dead-shows-dead (2026-08-21): the roster JOINS the spawner's LANE-4 death
certificate, so a stopped session reads EXITED instead of "running".

Background: AgentBeast posts POST /session/death on every stop (measured
17:15:20Z and 17:22:36Z the day this shipped). engram stored each cert in
the seat register — and /memory/roster never read it, so softphone-grok-2
sat on the list as state=running, stale, for hours after AB certified it
dead. The address register already consumed certs (REG-DEATH-1, with the
life-after-death void); the roster is the surface agents are told to consult,
and it was blind. These tests pin: cert → roster.death; life after died_at
voids it; recipient_liveness (send warnings) carries the same fact.
"""

from datetime import datetime, timedelta, timezone

import pytest

from server.services.memory_service import (
    DEATH_SCOPE,
    PRESENCE_NAMESPACE,
    SEAT_USER_ID,
    get_pool,
    presence_update,
    recipient_liveness,
    roster_list,
)
from server.services.session_registry import death_certify

PROJECT = "deathtest"


async def _cleanup():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope = 'presence' AND user_id = $1",
            PROJECT,
        )
        await conn.execute(
            "DELETE FROM memories WHERE namespace = $1 AND scope = $2 "
            "AND user_id = $3 AND project = $4",
            PRESENCE_NAMESPACE, DEATH_SCOPE, SEAT_USER_ID, PROJECT,
        )


async def _entry(identity: str) -> dict:
    entries = await roster_list(project=PROJECT, include_done=True)
    matches = [e for e in entries if e["identity"] == identity]
    assert matches, f"{identity} not on roster"
    return matches[0]


@pytest.mark.asyncio
async def test_certified_death_shows_on_roster(services):
    try:
        await presence_update(
            identity="deathtest-grok-2", project=PROJECT, state="running",
            provider="grok", session_nonce="n1", host="macmini",
        )
        before = await _entry("deathtest-grok-2")
        assert before["death"] is None
        # the spawner stops it and says so, a moment later
        died = datetime.now(timezone.utc) + timedelta(seconds=2)
        await death_certify(
            session_key="grok-abgrok-deathtest-abcd", seat="deathtest-grok-2",
            lane="deathtest-grok", project=PROJECT, provider="grok",
            host="macmini", died_at=died, cause="stopped", graceful=True,
            certified_by="agentbeast",
        )
        e = await _entry("deathtest-grok-2")
        assert e["death"] is not None
        assert e["death"]["certified_by"] == "agentbeast"
        assert e["death"]["cause"] == "stopped"
        assert e["death"]["died_at"].startswith(died.isoformat()[:19])
        # the row itself is untouched — facts added, nothing deleted or aged
        assert e["state"] == "running"
    finally:
        await _cleanup()


@pytest.mark.asyncio
async def test_life_after_death_voids_the_certificate(services):
    try:
        await presence_update(
            identity="deathtest-claude-3", project=PROJECT, state="running",
            provider="claude", session_nonce="n1", host="macmini",
        )
        # a cert whose died_at is in the PAST relative to the beat above:
        # something lived at this name after the "death" → void (reused name)
        died = datetime.now(timezone.utc) - timedelta(hours=1)
        await death_certify(
            session_key="grok-abgrok-deathtest-old1", seat="deathtest-claude-3",
            lane="deathtest-claude", project=PROJECT, provider="claude",
            host="macmini", died_at=died, cause="stopped", graceful=True,
            certified_by="agentbeast",
        )
        e = await _entry("deathtest-claude-3")
        assert e["death"] is None, "a beat after died_at must void the cert"
    finally:
        await _cleanup()


@pytest.mark.asyncio
async def test_recipient_liveness_carries_the_certificate(services):
    try:
        await presence_update(
            identity="deathtest-grok-4", project=PROJECT, state="running",
            provider="grok", session_nonce="n1", host="macmini",
        )
        died = datetime.now(timezone.utc) + timedelta(seconds=2)
        await death_certify(
            session_key="grok-abgrok-deathtest-ef01", seat="deathtest-grok-4",
            lane="deathtest-grok", project=PROJECT, provider="grok",
            host="macmini", died_at=died, cause="stopped", graceful=False,
            certified_by="agentbeast",
        )
        live = await recipient_liveness(["deathtest-grok-4"])
        info = live["deathtest-grok-4"]
        assert info["death"] is not None
        assert info["death"]["certified_by"] == "agentbeast"
        assert info["death"]["graceful"] is False
    finally:
        await _cleanup()


@pytest.mark.asyncio
async def test_cert_is_monotonic_on_died_at_under_a_stable_session_key(services):
    """EXIT-NOTICE-2: the cert key is the session_key, which SURVIVES restarts
    (SEAT-3 continuity). A plain first-writer-wins froze a session's FIRST
    death forever. A LATER died_at must replace the record; an earlier or
    equal one must not move it backwards; true repeats stay idempotent."""
    try:
        await presence_update(
            identity="deathtest-claude-5", project=PROJECT, state="running",
            provider="claude", session_nonce="n5", host="macmini",
        )
        base = datetime.now(timezone.utc) + timedelta(seconds=5)
        t1, t2, t0 = base, base + timedelta(minutes=10), base - timedelta(minutes=10)
        common = dict(session_key="grok-abgrok-deathtest-stable", seat="deathtest-claude-5",
                      lane="deathtest-claude", project=PROJECT, provider="claude",
                      host="macmini", graceful=True, certified_by="agentbeast")
        r = await death_certify(died_at=t1, cause="first-exit", **common)
        assert r["created"] is True and r["updated"] is False
        assert (await _entry("deathtest-claude-5"))["death"]["cause"] == "first-exit"
        # The session restarted and exited again, later: the cert follows.
        r = await death_certify(died_at=t2, cause="second-exit", **common)
        assert r["created"] is False and r["updated"] is True
        e = await _entry("deathtest-claude-5")
        assert e["death"]["cause"] == "second-exit"
        assert e["death"]["died_at"] == t2.isoformat()
        # A stale/late cert for an EARLIER death never moves it backwards.
        r = await death_certify(died_at=t0, cause="stale", **common)
        assert r["created"] is False and r["updated"] is False
        assert (await _entry("deathtest-claude-5"))["death"]["cause"] == "second-exit"
        # An exact repeat is a no-op too (idempotent).
        r = await death_certify(died_at=t2, cause="second-exit", **common)
        assert r["created"] is False and r["updated"] is False
    finally:
        await _cleanup()
