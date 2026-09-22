"""REPLY-TARGET-1 — a reply meant for one agent must not wake the whole lane.

LANE-5 routes a reply to the sender's LANE (`proj-codex`) so it survives the
sender's death. Every seat of that provider on the project listens on the
lane, so while the sender is alive a reply to it woke ALL of them. Measured
2026-09-22 overnight: 353 of 1,444 waking deliveries were this fan-out.

Rule under test: a reply aimed at the parent sender's lane goes to the
sender's own seat while that seat is live; stale / farewelled / never-seen
seats keep the lane (the case LANE-5 exists for).
"""

import pytest

PFX = "rt1test"
LANE = f"{PFX}-codex"
SEAT = f"{PFX}-codex-2"


async def _clean(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope IN ('inbox','presence')"
            " AND (user_id LIKE $1 OR key LIKE $2)",
            f"{PFX}%", f"presence/{PFX}%",
        )


async def _parent(client) -> str:
    r = await client.post("/memory/send", json={
        "to": f"{PFX}-codex-4", "subject": "review this",
        "body": "please audit abc123", "from_": f"{SEAT}@hosta",
        "from_lane": LANE, "intent": "action",
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _reply(client, parent_id: str, to: str = LANE):
    r = await client.post("/memory/send", json={
        "to": to, "subject": "re: review this", "body": "PASS abc123",
        "from_": f"{PFX}-codex-4@hosta", "in_reply_to": parent_id,
        "thread_id": parent_id, "intent": "action",
    })
    assert r.status_code == 200, r.text
    return r.json()


async def _recipient(db_pool, msg_id: str) -> str:
    async with db_pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT user_id FROM memories WHERE key = $1 AND scope = 'inbox'",
            msg_id,
        )


async def _heartbeat(client, seat=SEAT):
    r = await client.post("/memory/presence", json={
        "identity": seat, "project": PFX, "state": "running",
        "provider": "codex", "session_nonce": "n1",
    })
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_reply_to_live_sender_goes_to_its_seat_not_the_lane(client, db_pool):
    await _clean(db_pool)
    await _heartbeat(client)
    parent = await _parent(client)
    res = await _reply(client, parent)
    assert await _recipient(db_pool, res["id"]) == SEAT
    assert any("not its lane" in w for w in (res.get("recipient_warnings") or []))
    await _clean(db_pool)


@pytest.mark.asyncio
async def test_reply_to_never_seen_sender_keeps_the_lane(client, db_pool):
    """No presence row = cannot show the seat is listening → lane, as before."""
    await _clean(db_pool)
    parent = await _parent(client)
    res = await _reply(client, parent)
    assert await _recipient(db_pool, res["id"]) == LANE
    await _clean(db_pool)


@pytest.mark.asyncio
async def test_reply_to_departed_sender_keeps_the_lane(client, db_pool):
    """The sender said goodbye → the lane's next occupant must get it."""
    await _clean(db_pool)
    await _heartbeat(client)
    r = await client.post("/memory/presence", json={
        "identity": SEAT, "project": PFX, "state": "running",
        "provider": "codex", "session_nonce": "n1", "farewell": True,
    })
    assert r.status_code == 200, r.text
    parent = await _parent(client)
    res = await _reply(client, parent)
    assert await _recipient(db_pool, res["id"]) == LANE
    await _clean(db_pool)


@pytest.mark.asyncio
async def test_explicit_non_lane_target_is_left_alone(client, db_pool):
    """Only a reply aimed at the parent's lane is retargeted."""
    await _clean(db_pool)
    await _heartbeat(client)
    parent = await _parent(client)
    res = await _reply(client, parent, to=f"{PFX}-codex-5")
    assert await _recipient(db_pool, res["id"]) == f"{PFX}-codex-5"
    await _clean(db_pool)
