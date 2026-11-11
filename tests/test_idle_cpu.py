"""IDLE-CPU-1: the inbox/wait long-poll pushes its `since` cursor into SQL.

Before the fix every 2s poll fetched the newest 50 unread rows and dropped
the ones at/before the cursor in Python, so the query sorted the reader's
whole unread inbox each time. These tests pin that the cursor moved into the
SQL without changing what a waiter sees.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from server.services.memory_service import inbox_list, wake_list_fresh


ADDR = "idlecpu-probe"


async def _cleanup(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope IN ('inbox','wake') "
            "AND user_id LIKE 'idlecpu-%'")


async def _backdate(db_pool, key, when):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE memories SET created_at = $2 WHERE key = $1", key, when)


@pytest.mark.asyncio
async def test_wait_returns_mail_that_arrives_after_since(client, db_pool):
    await _cleanup(db_pool)
    # A deep unread backlog older than the cursor (> the 50-row window) must
    # neither be returned nor hide the new arrival.
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    for i in range(55):
        r = await client.post("/memory/send", json={
            "to": ADDR, "body": f"old-{i}", "from_": "peer@elsewhere"})
        await _backdate(db_pool, r.json()["id"], old + timedelta(seconds=i))

    async def send_later():
        await asyncio.sleep(0.3)
        return await client.post("/memory/send", json={
            "to": ADDR, "body": "arrived", "from_": "peer@elsewhere"})

    wait = client.post("/memory/inbox/wait", json={
        "listen_set": [ADDR], "reader_identity": f"{ADDR}@m",
        "timeout_seconds": 6})  # since defaults to request arrival
    r, sent = await asyncio.gather(wait, send_later())
    assert sent.status_code == 200
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "ok"
    assert [m["body"] for m in data["messages"]] == ["arrived"]
    await _cleanup(db_pool)


@pytest.mark.asyncio
async def test_wait_ignores_unread_mail_older_than_since(client, db_pool):
    await _cleanup(db_pool)
    for i in range(3):
        r = await client.post("/memory/send", json={
            "to": ADDR, "body": f"stale-{i}", "from_": "peer@elsewhere"})
        assert r.status_code == 200
    cursor = (datetime.now(timezone.utc) + timedelta(milliseconds=1)).isoformat()
    r = await client.post("/memory/inbox/wait", json={
        "listen_set": [ADDR], "reader_identity": f"{ADDR}@m",
        "timeout_seconds": 1, "since": cursor})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "timeout"
    assert r.json()["messages"] == []
    await _cleanup(db_pool)


@pytest.mark.asyncio
async def test_inbox_list_created_after(client, db_pool):
    await _cleanup(db_pool)
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    keys = []
    for i in range(6):
        r = await client.post("/memory/send", json={
            "to": ADDR, "body": f"m{i}", "from_": "peer@elsewhere"})
        keys.append(r.json()["id"])
        await _backdate(db_pool, keys[-1], base + timedelta(minutes=i))

    kw = dict(listen_set=[ADDR], reader_identity=f"{ADDR}@m",
              unread_only=True, limit=50, newest_first=True)
    everything = await inbox_list(**kw)
    assert [m.body for m in everything] == [f"m{i}" for i in reversed(range(6))]

    # None is the old behaviour, exactly.
    assert [m.id for m in await inbox_list(**kw, created_after=None)] == \
        [m.id for m in everything]

    # Strictly-after: the row AT the cursor is excluded, like the old filter.
    cut = base + timedelta(minutes=2)
    after = await inbox_list(**kw, created_after=cut)
    assert [m.body for m in after] == ["m5", "m4", "m3"]
    assert [m.id for m in after] == [
        m.id for m in everything if m.created_at > cut]

    # LIMIT interplay: rows newer than the cursor are a prefix of the DESC
    # order, so a small window keeps the same rows either way.
    small = dict(kw, limit=2)
    old_way = [m for m in await inbox_list(**small) if m.created_at > cut]
    assert [m.id for m in await inbox_list(**small, created_after=cut)] == \
        [m.id for m in old_way]

    assert await inbox_list(**kw, created_after=base + timedelta(hours=1)) == []
    await _cleanup(db_pool)


@pytest.mark.asyncio
async def test_wake_list_fresh_respects_since(client, db_pool):
    await _cleanup(db_pool)
    r = await client.post("/memory/wake", json={
        "to": ADDR, "ref": "huddle/old", "from_": "p"})
    old_id = r.json()["ids"][0]
    await _backdate(db_pool, old_id,
                    datetime.now(timezone.utc) - timedelta(minutes=10))
    r = await client.post("/memory/wake", json={
        "to": ADDR, "ref": "huddle/new", "from_": "p"})
    assert r.status_code == 200

    every = await wake_list_fresh([ADDR], reader_identity=f"{ADDR}@m")
    assert {w["ref"] for w in every} == {"huddle/old", "huddle/new"}

    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    fresh = await wake_list_fresh([ADDR], since=since,
                                  reader_identity=f"{ADDR}@m")
    assert [w["ref"] for w in fresh] == ["huddle/new"]

    assert await wake_list_fresh(
        [ADDR], since=datetime.now(timezone.utc) + timedelta(seconds=1),
        reader_identity=f"{ADDR}@m") == []
    await _cleanup(db_pool)


@pytest.mark.asyncio
async def test_created_at_index_is_in_the_schema(db_pool):
    async with db_pool.acquire() as conn:
        idx = await conn.fetchval(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'idx_memories_ns_scope_uid_created'")
    assert idx is not None
    assert "(namespace, scope, user_id, created_at DESC)" in idx
