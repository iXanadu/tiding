"""OWNER-LIVENESS-1: a DM to the owner must not carry a dead-recipient
warning borrowed from a PROJECT of the same name.

Measured 2026-10-07: the owner's address equals a project's name, the
project's last seat had stopped heartbeating 142h earlier, and every
action DM to the owner came back "freshest listener <project>-claude-2:
last heartbeat 142h ago — do not expect a reply". Agents relayed that the
owner was unreachable while he was answering DMs hourly.
"""

import pytest

from server.config import settings

OWNER = "ownerperson"


@pytest.mark.asyncio
async def test_owner_dm_has_no_borrowed_liveness_warning(
    client, db_pool, monkeypatch
):
    monkeypatch.setattr(settings, "owner_principal_name", OWNER)
    seat = f"{OWNER}-claude-2"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='presence' AND user_id=$1", OWNER)
    await client.post("/memory/presence", json={
        "identity": seat, "project": OWNER, "state": "running"})
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE memories SET last_used_at = NOW() - interval '142 hours' "
            "WHERE scope='presence' AND user_id=$1", OWNER)

    r = await client.post("/memory/send", json={
        "to": OWNER, "from_": "somepeer", "intent": "action",
        "subject": "decision needed", "body": "yes or no?"})
    assert r.status_code == 200
    assert not r.json().get("recipient_warnings"), r.json()

    # The project's own seat address is still judged honestly.
    r2 = await client.post("/memory/send", json={
        "to": seat, "from_": "somepeer", "intent": "action",
        "subject": "work", "body": "for the seat"})
    assert r2.json().get("recipient_warnings")

    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='presence' AND user_id=$1", OWNER)
        await conn.execute(
            "DELETE FROM memories WHERE scope='inbox' AND user_id = ANY($1)",
            [OWNER, seat])
