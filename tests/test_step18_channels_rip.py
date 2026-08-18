"""Step 18 — the #channels rip (huddle 3ln8Ck05, scored v2 design).

Broadcast '#channels' were launch-time subscription lists that O2's project
channels replaced. The rip refuses '#' mail at the door (LOUD, per WIRE-1 —
a deployed writer that still targets '#' must fail visibly, never silently
drop) and serves presence/roster `channels` as an inert empty field (kept on
the wire so deployed readers keep parsing; removing a served field is the
WIRE-1 breakage class).
"""

import pytest


@pytest.mark.asyncio
async def test_hash_send_refused_with_guidance(client):
    r = await client.post("/memory/send", json={
        "to": "#devagents", "subject": "s", "body": "b",
    })
    assert r.status_code == 409
    detail = r.json()["detail"]
    # Hole 2: the guidance must NOT pretend a rename — #devagents was
    # box-wide; no single project channel is its equivalent.
    assert "#channels are retired" in detail
    assert "NO one-project equivalent" in detail
    assert "#devagents" in detail


@pytest.mark.asyncio
async def test_fanout_containing_hash_refused_whole(client):
    """One '#' target poisons the whole send — a partial delivery would be a
    silent drop of the '#' leg, which is exactly what the rip must not do."""
    r = await client.post("/memory/send", json={
        "to": ["someproject", "#devagents"], "subject": "s", "body": "b",
    })
    assert r.status_code == 409
    assert "#devagents" in r.json()["detail"]


@pytest.mark.asyncio
async def test_plain_send_unaffected(client):
    r = await client.post("/memory/send", json={
        "to": "someproject", "subject": "s", "body": "b",
    })
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_roster_serves_channels_inert(client, db_pool):
    """The presence PARAM is still accepted (deployed bridges send it); the
    served field is pinned []. Field present = deployed readers keep parsing."""
    await client.post("/memory/presence", json={
        "identity": "ripper", "project": "ripper", "state": "running",
        "provider": "grok", "channels": ["#devagents"], "session_nonce": "rn1",
    })
    try:
        r = await client.post("/memory/roster", json={})
        entries = [e for e in r.json()["entries"] if e["identity"] == "ripper"]
        assert entries, "presence row missing from roster"
        assert entries[0]["channels"] == []
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM memories WHERE key = 'presence/ripper'")


@pytest.mark.asyncio
async def test_roster_channel_filter_matches_nothing(client, db_pool):
    """The filter keys on data no longer served, so it is honest-inert:
    matching only stale pre-rip rows would be worse than matching none."""
    await client.post("/memory/presence", json={
        "identity": "ripper2", "project": "ripper2", "state": "running",
        "provider": "grok", "channels": ["#devagents"], "session_nonce": "rn2",
    })
    try:
        r = await client.post("/memory/roster", json={"channel": "#devagents"})
        assert r.json()["entries"] == []
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM memories WHERE key = 'presence/ripper2'")
