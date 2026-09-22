"""LAST-SPOKE-IS-THE-WATCHER-1: tell an agent's turn from its watcher's beat.

WHAT THIS COST, 2026-09-22. The owner had eleven agents parked, blocked on an
allocation reset, and had twice called them critical. Asked whether they were
alive, I read the roster: "spoke 5s ago", five of five, all fresh. I told him
his team was fine. He disproved it from a screenshot of one agent's own last
turn — which had FAILED at 07:42, ten hours earlier.

The reading was not careless. `last_used_at` is refreshed by the WATCHER as
well as by the agent, deliberately (SEAT-7: so a session head-down on a long
build is not aged out and reclaimed mid-work). The consequence is that the
column every reader takes for agent activity also reports watcher liveness,
and afterwards the two are indistinguishable. The roster prints "last spoke"
and "watcher beat" side by side as if they corroborate; they cannot, because
one is derived from the other.

I had written the lesson naming this exact distinction earlier the same day
and quoted it to the owner two hours before. That is the measure of how
convincing the display is, not of how careless the reader was — which is why
the fix is a separate field and not a note telling people to be careful.

`agent_last_active` is stamped ONLY on the agent path, on a real tool call.
The watcher's beat merges one key with jsonb_set and cannot touch it.
"""

import asyncio

import pytest

from server.services.memory_service import (
    PRESENCE_NAMESPACE,
    get_pool,
    presence_update,
    presence_watcher_beat,
)

IDENT = "activityprobe-claude"
PROJECT = "activityprobe"


async def _row_md():
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT metadata, last_used_at FROM memories "
            "WHERE namespace = $1 AND scope = 'presence' AND key = $2",
            PRESENCE_NAMESPACE, f"presence/{IDENT}",
        )
    if r is None:
        return None, None
    md = r["metadata"]
    if isinstance(md, str):
        import json
        md = json.loads(md)
    return (md or {}), r["last_used_at"]


async def _cleanup():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE namespace = $1 AND scope = 'presence' "
            "AND key = $2",
            PRESENCE_NAMESPACE, f"presence/{IDENT}",
        )


async def test_a_watcher_beat_does_not_make_an_agent_look_busy(services):
    """THE FAILURE, REPRODUCED. Agent acts once, then goes silent while its
    watcher keeps beating — exactly the owner's stalled team.

    `last_used_at` MUST keep advancing (that is SEAT-7 protecting a
    long-running session from reclamation, and removing it would trade this
    bug for a worse one). `agent_last_active` MUST NOT, because the agent did
    nothing.
    """
    await _cleanup()
    try:
        await presence_update(
            identity=IDENT, project=PROJECT, state="running",
            provider="claude", host="testhost", session_nonce="n1",
        )
        md_before, used_before = await _row_md()
        agent_stamp = md_before.get("agent_last_active")
        assert agent_stamp, "the agent path must stamp its own activity"

        await asyncio.sleep(1.1)

        # The agent does NOTHING from here. Only its watcher beats.
        await presence_watcher_beat(identity=IDENT, project=PROJECT)
        md_after, used_after = await _row_md()

        assert used_after > used_before, (
            "the watcher must still refresh last_used_at — SEAT-7 keeps a "
            "busy session from being aged out mid-work"
        )
        assert md_after.get("agent_last_active") == agent_stamp, (
            "a watcher beat must NOT advance the agent's activity stamp; "
            "if it does, this field is as misleading as the one it replaces"
        )
    finally:
        await _cleanup()


async def test_the_two_facts_are_separable_after_the_fact(services):
    """The property the roster needs: a reader can ask 'is this AGENT
    working' and get an answer that a watcher cannot forge."""
    await _cleanup()
    try:
        await presence_update(
            identity=IDENT, project=PROJECT, state="running",
            provider="claude", host="testhost", session_nonce="n1",
        )
        first, _ = await _row_md()
        await asyncio.sleep(1.1)
        for _ in range(3):
            await presence_watcher_beat(identity=IDENT, project=PROJECT)
        quiet, _ = await _row_md()
        assert quiet.get("agent_last_active") == first.get("agent_last_active")

        # Now the agent genuinely acts again.
        await presence_update(
            identity=IDENT, project=PROJECT, state="running",
            provider="claude", host="testhost", session_nonce="n1",
        )
        active, _ = await _row_md()
        assert active.get("agent_last_active") != first.get("agent_last_active"), (
            "a real tool call must advance it"
        )
    finally:
        await _cleanup()


async def test_the_bridges_keepalive_timer_does_not_make_an_agent_look_busy(services):
    """AGENT-ACTIVE-1, same day, same failure by a second route. The BRIDGE
    also beats on a 120s timer so an idle session stays on the picker — and
    that beat went through the agent path, so every idle agent (even one out
    of allowance) read "active a minute ago" forever. Measured live: four
    exhausted Codex seats all stamped within 30s of each other.

    A timer beat (activity=False) must carry the stamp forward untouched; a
    real tool-call beat (activity=True, the default) must advance it.
    """
    await _cleanup()
    try:
        await presence_update(
            identity=IDENT, project=PROJECT, state="running",
            provider="claude", host="testhost", session_nonce="n1",
        )
        md0, _ = await _row_md()
        stamp = md0.get("agent_last_active")
        assert stamp

        await asyncio.sleep(1.1)
        await presence_update(
            identity=IDENT, project=PROJECT, state="running",
            provider="claude", host="testhost", session_nonce="n1",
            activity=False,
        )
        md1, _ = await _row_md()
        assert md1.get("agent_last_active") == stamp, (
            "a keep-alive timer beat must NOT advance the agent's activity"
        )

        await presence_update(
            identity=IDENT, project=PROJECT, state="running",
            provider="claude", host="testhost", session_nonce="n1",
        )
        md2, _ = await _row_md()
        assert md2.get("agent_last_active") > stamp, (
            "a real tool-call beat must advance it"
        )
    finally:
        await _cleanup()
