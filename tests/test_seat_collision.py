"""Seat-collision detection: two live sessions on one inbox identity.

Grew out of ROST-1. A session nonce rides each presence heartbeat; the server
flags an identity whose nonce map holds >1 fresh nonce (the silent "two
bodies, one seat" misconfiguration), exempting deliberately shared roles.
"""

import json as _json

import pytest as _pytest


@_pytest.mark.asyncio
async def test_two_nonces_one_identity_flags_collision(client, db_pool):
    r1 = await client.post("/memory/presence", json={
        "identity": "collidey", "project": "collidey", "state": "running",
        "provider": "claude", "session_nonce": "nonceAAA",
    })
    assert r1.status_code == 200
    assert r1.json()["collision"] is None  # first session: clear

    r2 = await client.post("/memory/presence", json={
        "identity": "collidey", "project": "collidey", "state": "running",
        "provider": "claude", "session_nonce": "nonceBBB",
    })
    assert r2.status_code == 200
    col = r2.json()["collision"]
    assert col is not None
    assert col["live_sessions"] == 2
    assert col["providers"] == ["claude"]  # same-provider collision IS caught

    # roster surfaces it
    r3 = await client.post("/memory/roster", json={"project": "collidey"})
    entry = [e for e in r3.json()["entries"] if e["identity"] == "collidey"][0]
    assert entry["collision"] is True
    assert entry["live_sessions"] == 2

    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE key = 'presence/collidey'")


@_pytest.mark.asyncio
async def test_admin_identity_exempt_from_collision(client, db_pool):
    for nonce in ("adm1", "adm2"):
        r = await client.post("/memory/presence", json={
            "identity": "admin", "project": "admin", "state": "running",
            "provider": "claude" if nonce == "adm1" else "grok",
            "session_nonce": nonce,
        })
        assert r.status_code == 200
        assert r.json()["collision"] is None  # role-sharing by design

    r3 = await client.post("/memory/roster", json={"project": "admin"})
    entry = [e for e in r3.json()["entries"] if e["identity"] == "admin"][0]
    assert entry["collision"] is False
    assert entry["live_sessions"] == 2  # visible, just not flagged

    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE key = 'presence/admin'")


@_pytest.mark.asyncio
async def test_legacy_client_without_nonce_never_collides(client, db_pool):
    for _ in range(2):
        r = await client.post("/memory/presence", json={
            "identity": "legacyseat", "project": "legacyseat", "state": "running",
            "provider": "claude",
        })
        assert r.status_code == 200
        assert r.json()["collision"] is None

    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE key = 'presence/legacyseat'")


@_pytest.mark.asyncio
async def test_stale_nonce_pruned_no_false_collision(client, db_pool):
    """A bridge restart (old nonce gone quiet) must clear within the window."""
    await client.post("/memory/presence", json={
        "identity": "restarty", "project": "restarty", "state": "running",
        "provider": "claude", "session_nonce": "oldnonce",
    })
    # age the old nonce beyond the collision window, directly in the row
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT metadata FROM memories WHERE key = 'presence/restarty'")
        md = row["metadata"]
        if isinstance(md, str):
            md = _json.loads(md)
        md["sessions"]["oldnonce"]["last_seen"] = "2020-01-01T00:00:00+00:00"
        await conn.execute(
            "UPDATE memories SET metadata = $1::jsonb WHERE key = 'presence/restarty'",
            _json.dumps(md),
        )
    r = await client.post("/memory/presence", json={
        "identity": "restarty", "project": "restarty", "state": "running",
        "provider": "claude", "session_nonce": "newnonce",
    })
    assert r.json()["collision"] is None  # stale nonce pruned, single seat again

    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE key = 'presence/restarty'")


@_pytest.mark.asyncio
async def test_a_displaced_nonces_own_beat_cannot_reflag_collision(
        client, db_pool):
    """T2 (succession ≠ collision): SEAT-12 filtered corpses on READ, but a
    displaced predecessor's own dying-tail beat re-inserted its nonce — so a
    clean succession oscillated into a 2-live collision for as long as the
    old bridge kept beating. Measured live 2026-08-18: the successor obeyed
    the banner and minted an ordinal to escape a corpse. The write door must
    apply the same rule as the read door.
    """
    from server.services.memory_service import PRESENCE_NAMESPACE
    ident, proj = "t2probe", "t2proj"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='presence' AND user_id=$1", proj)
        await conn.execute(
            "DELETE FROM memories WHERE scope='seat' AND project=$1", proj)

    # Succession: gen2 holds the seat; gen1 is recorded displaced.
    await client.post("/memory/presence", json={
        "identity": ident, "project": proj, "state": "running",
        "session_nonce": "gen2"})
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO memories (namespace, key, value, scope, user_id, project,
                                  tags, tags_search, metadata)
            VALUES ($1, $2, 'seat', 'seat', 'global', $3, '', '',
                    jsonb_build_object('superseded_nonces',
                                       jsonb_build_array('gen1')))
            ON CONFLICT (namespace, key, scope, user_id, project) DO UPDATE
              SET metadata = EXCLUDED.metadata
            """,
            PRESENCE_NAMESPACE, f"seat/{ident}", proj,
        )

    # The dying tail beats — its OWN write must not re-add it.
    r = await client.post("/memory/presence", json={
        "identity": ident, "project": proj, "state": "running",
        "session_nonce": "gen1"})
    assert not r.json().get("collision"), (
        "the corpse's own beat re-flagged the succession as a collision"
    )
    # And the successor's next beat still sees exactly one live session.
    r = await client.post("/memory/presence", json={
        "identity": ident, "project": proj, "state": "running",
        "session_nonce": "gen2"})
    assert not r.json().get("collision")
    r = await client.post("/memory/roster", json={"project": proj})
    entry = next(e for e in r.json()["entries"] if e["identity"] == ident)
    assert entry["live_sessions"] == 1
    assert entry["collision"] is False

    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='presence' AND user_id=$1", proj)
        await conn.execute(
            "DELETE FROM memories WHERE scope='seat' AND project=$1", proj)
