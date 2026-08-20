"""Step 14: epoch expiry for deep chatter (O5 — depth is fragility).

Chatter only, deep only, reversible resolve. Asks never swept; roots never
swept; fresh deep chatter survives; a dead incarnation's chatter is spent
whatever its age.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from server.services.memory_service import PRESENCE_NAMESPACE
from server.services.session_registry import (
    DEEP_CHATTER_EPOCH_SECONDS,
    SEAT_GRACE_SECONDS,
)

P = "sw14proj"


async def _clear(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope IN "
            "('seat','death','project-root') AND project LIKE $1", f"{P}%")
        await conn.execute(
            "DELETE FROM memories WHERE scope='inbox' AND user_id LIKE $1",
            f"{P}%")


async def _register_root(client):
    r = await client.post("/session/claim", json={
        "session_key": "sw14-rootmaker", "project": P, "provider": "gpt"})
    assert r.status_code == 200


async def _mail(client, to, intent=None, aged=False, db_pool=None):
    body = {"to": to, "body": "b", "subject": "s", "from_": "someone"}
    if intent:
        body["intent"] = intent
    r = await client.post("/memory/send", json=body)
    assert r.status_code == 200
    mid = r.json()["id"]
    if aged:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """UPDATE memories SET created_at =
                   NOW() - ($1 || ' seconds')::interval WHERE key = $2""",
                str(DEEP_CHATTER_EPOCH_SECONDS + 3600), mid)
    return mid


@pytest.mark.asyncio
async def test_sweep_matrix(client, db_pool):
    await _clear(db_pool)
    await _register_root(client)
    dead = f"{P}-claude-3"
    # died_at MUST be derived from the same clock as the seat row's age, and
    # must fall AFTER it. address_register voids a death that the seat outlived
    # ("someone lived here after the death", session_registry ~L1561), so a
    # FIXED died_at silently stops meaning "dead" the moment wall-clock carries
    # the relative seat age past it. A hardcoded 2026-08-13 did exactly that:
    # this test was green for weeks and began failing at 2026-08-20T00:00Z, when
    # NOW() - (SEAT_GRACE + 9000s) first landed later than the constant. The
    # production voiding logic was right the whole time; only the fixture rotted.
    died_at = (datetime.now(timezone.utc)
               - timedelta(seconds=SEAT_GRACE_SECONDS + 3600)).isoformat()
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO memories (namespace,key,value,scope,user_id,project,
               tags,tags_search,metadata,last_used_at)
               VALUES ($1,$2,'seat','seat','global',$3,'','',$4::jsonb,
                       NOW() - ($5 || ' seconds')::interval)""",
            PRESENCE_NAMESPACE, f"seat/{dead}", P,
            json.dumps({"session_key": "sw14dead"}),
            str(SEAT_GRACE_SECONDS + 9000))
        await conn.execute(
            """INSERT INTO memories (namespace,key,value,scope,user_id,project,
               tags,tags_search,metadata)
               VALUES ($1,$2,'death','death','global',$3,'','',$4::jsonb)""",
            PRESENCE_NAMESPACE, f"death/{uuid.uuid4()}", P,
            json.dumps({"session_key": "sw14dead", "seat": dead,
                        "died_at": died_at,
                        "cause": "stop", "graceful": True,
                        "certified_by": "t"}))

    dead_chatter = await _mail(client, dead)                     # fresh but dead node
    old_chatter = await _mail(client, f"{P}-grok-2", aged=True,
                              db_pool=db_pool)                   # aged deep
    fresh_chatter = await _mail(client, f"{P}-grok-2")           # fresh deep
    root_chatter = await _mail(client, P, aged=True,
                               db_pool=db_pool)                  # aged ROOT
    deep_ask = await _mail(client, dead, intent="action")        # ask on dead node

    r = await client.post("/admin/inbox/sweep")
    assert r.status_code == 200
    body = r.json()
    swept = {s["id"]: s["reason"] for s in body["swept"]}
    assert swept.get(dead_chatter) == "incarnation-dead"
    assert swept.get(old_chatter) == "older-than-epoch"
    assert fresh_chatter not in swept, "fresh deep chatter survives"
    assert root_chatter not in swept, "root mail is never swept"
    assert deep_ask not in swept, "asks are NEVER swept — climb owns them"
    assert body["skipped"]["ask"] >= 1
    assert body["skipped"]["root"] >= 1
    assert body["skipped"]["fresh"] >= 1

    # Swept rows are RESOLVED (reversible), not archived — and the ask can
    # still climb afterward.
    r = await client.post("/memory/inbox", json={
        "listen_set": [dead], "reader_identity": "x@x",
        "unread_only": False, "include_resolved": True})
    m = next(x for x in r.json()["messages"] if x["id"] == dead_chatter)
    assert m["status"] == "resolved"
    assert m["resolved_by"] == "system:epoch-sweep"
    r = await client.post("/admin/inbox/climb")
    assert any(c["id"] == deep_ask for c in r.json()["climbed"]), (
        "the surviving ask climbs off the dead incarnation"
    )
    await _clear(db_pool)
