"""Step 13: climb — unhandled asks rise to the nearest living ancestor.

O5's one exception to depth-is-ephemeral, with the three locks: later-life
(or live occupancy) voids a cert (REG-DEATH-1); lane dormancy is a dwell
window, not a snapshot; exempt roles never climb. Never on a guess:
handled=UNKNOWN holds.
"""

import json
import uuid

import pytest

from server.services.memory_service import PRESENCE_NAMESPACE
from server.services.session_registry import (
    CLIMB_LANE_DWELL_SECONDS,
    SEAT_GRACE_SECONDS,
)

P = "cl13proj"


async def _clear(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope IN "
            "('seat','death','presence','project-root') AND project LIKE $1",
            f"{P}%")
        await conn.execute(
            "DELETE FROM memories WHERE scope='inbox' AND user_id LIKE $1",
            f"{P}%")


async def _seat(db_pool, name, age_seconds=0, session_key=None):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO memories (namespace, key, value, scope, user_id,
                                  project, tags, tags_search, metadata,
                                  last_used_at)
            VALUES ($1, $2, 'seat', 'seat', 'global', $3, '', '',
                    $4::jsonb, NOW() - ($5 || ' seconds')::interval)
            ON CONFLICT (namespace, key, scope, user_id, project) DO UPDATE
              SET metadata = EXCLUDED.metadata,
                  last_used_at = EXCLUDED.last_used_at
            """,
            PRESENCE_NAMESPACE, f"seat/{name}", P,
            json.dumps({"session_key": session_key or f"key-{name}"}),
            str(age_seconds),
        )


async def _cert(db_pool, seat, session_key, died_ago_seconds):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO memories (namespace, key, value, scope, user_id,
                                  project, tags, tags_search, metadata)
            VALUES ($1, $2, 'death', 'death', 'global', $3, '', '', $4::jsonb)
            """,
            PRESENCE_NAMESPACE, f"death/{uuid.uuid4()}", P,
            json.dumps({
                "session_key": session_key, "seat": seat,
                "died_at": "2026-08-17T00:00:00+00:00"
                if died_ago_seconds is None else None,
                "cause": "stop", "graceful": True,
                "certified_by": "test-spawner",
            }),
        )


async def _ask(client, to, thread_id=None):
    body = {"to": to, "body": "please act", "subject": "ask",
            "intent": "action", "from_": f"{P}asker-claude-2",
            "from_lane": f"{P}asker-claude"}
    if thread_id:
        body["thread_id"] = thread_id
    r = await client.post("/memory/send", json=body)
    assert r.status_code == 200
    return r.json()["id"]


async def _register_root(client):
    r = await client.post("/session/claim", json={
        "session_key": "cl13-rootmaker", "project": P, "provider": "gpt"})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_regdeath1_live_holder_voids_cert_dead_row_keeps_it(
        client, db_pool):
    """Lock 1, both directions — tonight's own row is the fixture shape."""
    await _clear(db_pool)
    live, dead = f"{P}-claude-2", f"{P}-claude-3"
    await _seat(db_pool, live, age_seconds=5, session_key="slotkey")
    await _seat(db_pool, dead, age_seconds=SEAT_GRACE_SECONDS + 9000,
                session_key="deadkey")
    await _cert(db_pool, live, "slotkey", died_ago_seconds=None)
    await _cert(db_pool, dead, "deadkey", died_ago_seconds=None)
    r = await client.get("/session/addresses", params={"project": P})
    reg = {e["address"]: e for e in r.json()["entries"]}
    assert reg[live]["death"] is None, (
        "a live-holder row must never carry a cert — REG-DEATH-1"
    )
    assert reg[dead]["death"] is not None, (
        "a genuinely dead row keeps its evidence"
    )
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_unhandled_ask_on_dead_incarnation_climbs_one_level(
        client, db_pool):
    await _clear(db_pool)
    await _register_root(client)
    dead = f"{P}-claude-3"
    await _seat(db_pool, dead, age_seconds=SEAT_GRACE_SECONDS + 9000,
                session_key="deadkey")
    await _cert(db_pool, dead, "deadkey", died_ago_seconds=None)
    ask = await _ask(client, dead)

    r = await client.post("/admin/inbox/climb")
    assert r.status_code == 200
    body = r.json()
    assert any(c["id"] == ask and c["to"] == f"{P}-claude"
               for c in body["climbed"]), body

    # Same id, new address, provenance recorded; discriminator still works.
    r = await client.post("/memory/inbox", json={
        "listen_set": [f"{P}-claude"], "reader_identity": "lane@x",
        "unread_only": False})
    m = next(x for x in r.json()["messages"] if x["id"] == ask)
    assert m["to"] == f"{P}-claude"
    assert m["handled"] is False  # still an open unhandled ask, now at the lane
    # Answer it at the new address — handled flips.
    await client.post("/memory/send", json={
        "to": f"{P}asker", "body": "done", "subject": "re: ask",
        "intent": "action", "from_": f"{P}-claude-4",
        "from_lane": f"{P}-claude", "in_reply_to": ask, "thread_id": ask})
    r = await client.post("/memory/inbox", json={
        "listen_set": [f"{P}-claude"], "reader_identity": "lane@x",
        "unread_only": False})
    m = next(x for x in r.json()["messages"] if x["id"] == ask)
    assert m["handled"] is True and m["handled_via"] == "replied"
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_handled_unknown_live_and_exempt_never_climb(client, db_pool):
    await _clear(db_pool)
    await _register_root(client)
    dead = f"{P}-claude-3"
    await _seat(db_pool, dead, age_seconds=SEAT_GRACE_SECONDS + 9000,
                session_key="deadkey")
    await _cert(db_pool, dead, "deadkey", died_ago_seconds=None)

    handled_ask = await _ask(client, dead)
    await client.post("/memory/send", json={
        "to": f"{P}asker", "body": "answered", "subject": "re",
        "intent": "action", "from_": "someoneelse-claude-2",
        "from_lane": "someoneelse-claude", "in_reply_to": handled_ask})
    unknown_ask = await _ask(client, dead, thread_id="inbox/old-thread")
    live = f"{P}-claude-2"
    await _seat(db_pool, live, age_seconds=5)
    live_ask = await _ask(client, live)

    r = await client.post("/admin/inbox/climb")
    ids = [c["id"] for c in r.json()["climbed"]]
    assert handled_ask not in ids, "handled never climbs"
    assert unknown_ask not in ids, "UNKNOWN never climbs — no guessing"
    assert live_ask not in ids, "a live holder keeps its mail"
    sk = r.json()["skipped"]
    assert sk["handled"] >= 1 and sk["unknown"] >= 1 and sk["holder_alive"] >= 1
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_lane_dormancy_is_a_window_and_needs_live_project(
        client, db_pool):
    await _clear(db_pool)
    await _register_root(client)  # gpt seat: fresh — the ACTIVE ancestor
    lane = f"{P}-claude"
    # Lane subtree silent far past the dwell window.
    await _seat(db_pool, f"{P}-claude-2",
                age_seconds=CLIMB_LANE_DWELL_SECONDS + 3600)
    lane_ask = await _ask(client, lane)
    r = await client.post("/admin/inbox/climb")
    assert any(c["id"] == lane_ask and c["to"] == P
               and c["reason"] == "lane-dormant-while-project-active"
               for c in r.json()["climbed"]), r.json()

    # A lane quiet for LESS than the dwell holds (succession gap, Lock 2).
    await _seat(db_pool, f"{P}-grok-2", age_seconds=120)
    brief_ask = await _ask(client, f"{P}-grok")
    r = await client.post("/admin/inbox/climb")
    assert brief_ask not in [c["id"] for c in r.json()["climbed"]]
    assert r.json()["skipped"]["lane_active"] >= 1
    await _clear(db_pool)
