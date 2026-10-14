"""REPLY-THREAD-1: a send carrying only `in_reply_to` joins its parent's
thread. The bridge computed this client-side, so any other client (the
emergency Telegram bot, 2026-10-14) sent replies with thread_id=null."""
import json

import pytest


async def _thread(db_pool, mid):
    async with db_pool.acquire() as conn:
        md = await conn.fetchval("SELECT metadata FROM memories WHERE key=$1", mid)
    md = json.loads(md) if isinstance(md, str) else md
    return md.get("thread_id")


async def _cleanup(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='inbox' AND value LIKE 'rt1-%'")


async def _send(client, **kw):
    r = await client.post("/memory/send", json={
        "subject": "s", "from_": "rt1-a", "intent": "fyi", **kw})
    assert r.status_code == 200, r.text
    return r.json()["id"] if "id" in r.json() else r.json()["ids"][0]


@pytest.mark.asyncio
async def test_reply_joins_parent_thread(client, db_pool):
    await _cleanup(db_pool)
    try:
        root = await _send(client, to="rt1-b", body="rt1-root")
        # parent with no thread: the reply's thread is the parent's id
        r1 = await _send(client, to="rt1-a", body="rt1-r1", in_reply_to=root)
        assert await _thread(db_pool, r1) == root
        # parent inside a thread: the reply joins that thread
        r2 = await _send(client, to="rt1-b", body="rt1-r2", in_reply_to=r1)
        assert await _thread(db_pool, r2) == root
        # an explicit thread_id always wins
        r3 = await _send(client, to="rt1-b", body="rt1-r3", in_reply_to=r1,
                         thread_id="inbox/rt1-explicit")
        assert await _thread(db_pool, r3) == "inbox/rt1-explicit"
        # unknown parent: sent, unthreaded, never an error
        r4 = await _send(client, to="rt1-b", body="rt1-r4",
                         in_reply_to="inbox/rt1-no-such")
        assert await _thread(db_pool, r4) is None
    finally:
        await _cleanup(db_pool)
