"""PRES-2: the machine axis on the presence beat.

The seat-exempt admin role has no seat row to join a host from, and presence
never carried one — so the store could not say WHICH box an admin session was
on, while the doctrine called the machine axis "automatic." These tests pin
the fix: host rides the beat, survives legacy beats, and multi-box shared
roles serve an honest hosts_seen set instead of a last-writer-wins lie.
"""

import pytest

from server.services.memory_service import (
    get_pool,
    presence_update,
    roster_list,
)

PROJECT = "pres2test"


async def _cleanup():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope = 'presence' AND user_id = $1",
            PROJECT,
        )


async def _entry(identity: str) -> dict:
    entries = await roster_list(project=PROJECT)
    matches = [e for e in entries if e["identity"] == identity]
    assert matches, f"{identity} not on roster"
    return matches[0]


@pytest.mark.asyncio
async def test_host_served_on_roster(services):
    try:
        await presence_update(
            identity="pres2-solo", project=PROJECT, state="running",
            provider="claude", session_nonce="n1", host="webone",
        )
        e = await _entry("pres2-solo")
        assert e["host"] == "webone"
        assert e["hosts_seen"] == ["webone"]
    finally:
        await _cleanup()


@pytest.mark.asyncio
async def test_legacy_beat_does_not_wipe_host(services):
    """A pre-PRES-2 bridge sends no host; its beat must not erase the host a
    newer client recorded (MSG-9 wholesale-replace rule)."""
    try:
        await presence_update(
            identity="pres2-legacy", project=PROJECT, state="running",
            session_nonce="n1", host="dbone",
        )
        await presence_update(
            identity="pres2-legacy", project=PROJECT, state="running",
            session_nonce="n1",  # no host — legacy client
        )
        e = await _entry("pres2-legacy")
        assert e["host"] == "dbone"
    finally:
        await _cleanup()


@pytest.mark.asyncio
async def test_shared_role_multi_box_hosts_seen(services):
    """The admin case: ONE presence row, sessions on several boxes. The
    top-level host is last-beater-wins by construction; hosts_seen must carry
    the honest multi-box set so renderers qualify per (identity, host)."""
    try:
        await presence_update(
            identity="admin", project=PROJECT, state="running",
            provider="claude", session_nonce="mac-1", host="macmini",
        )
        await presence_update(
            identity="admin", project=PROJECT, state="running",
            provider="grok", session_nonce="web-1", host="webone",
        )
        e = await _entry("admin")
        assert e["host"] == "webone"  # last beater — documented, not trusted alone
        assert e["hosts_seen"] == ["macmini", "webone"]
        # admin stays collision-exempt: two live sessions, no collision flag.
        assert e["live_sessions"] == 2
        assert e["collision"] is False
    finally:
        await _cleanup()
