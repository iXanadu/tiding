"""Delivery is STRUCTURAL; ack is voluntary.

Owner's ask, 2026-08-20: "can we make acks structural — or 'delivered'
structural". The answer is yes for delivered, and it matters because ack
turned out to measure provider manners rather than transport. Measured that
day: cursor acked 6 of 6 inbound over 18h; grok acked essentially none; BOTH
were reading and replying. Keying an alarm on ack produced a false fleet-wide
claim that grok sessions were deaf.

The server knows when it hands a message to a reader. That needs no
cooperation and cannot be forgotten.

LIMIT, kept explicit in the tests: delivered means the bytes went into a tool
result. Whether the model attended to them is NOT observable, and this field
must never be read as "seen".
"""

import uuid

import pytest


async def _send(client, to, subject="delivery probe"):
    r = await client.post("/memory/send", json={
        "to": to, "subject": subject, "body": "x", "intent": "action",
    })
    assert r.status_code == 200
    return r.json()["id"]


async def _delivered_to(db_pool, mid):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT metadata->'delivered_to' AS d FROM memories WHERE key=$1", mid)
    import json as _j
    raw = row["d"] if row else None
    if raw is None:
        return []
    return _j.loads(raw) if isinstance(raw, str) else list(raw)


@pytest.mark.asyncio
async def test_reading_an_inbox_records_delivery_without_any_ack(client, db_pool):
    addr = f"deliv-{uuid.uuid4().hex[:8]}"
    mid = await _send(client, addr)

    assert await _delivered_to(db_pool, mid) == [], "nothing delivered before a read"

    r = await client.post("/memory/inbox", json={
        "listen_set": [addr], "reader_identity": f"{addr}@macmini", "limit": 10,
    })
    assert r.status_code == 200

    # No ack was ever called. Delivery must be recorded anyway — that is the
    # entire point: it does not depend on the agent doing anything.
    assert f"{addr}@macmini" in await _delivered_to(db_pool, mid)


@pytest.mark.asyncio
async def test_delivery_is_idempotent_across_repeated_reads(client, db_pool):
    addr = f"deliv-idem-{uuid.uuid4().hex[:8]}"
    mid = await _send(client, addr)
    body = {"listen_set": [addr], "reader_identity": f"{addr}@macmini",
            "unread_only": False, "limit": 10}

    for _ in range(3):
        assert (await client.post("/memory/inbox", json=body)).status_code == 200

    delivered = await _delivered_to(db_pool, mid)
    assert delivered.count(f"{addr}@macmini") == 1, (
        f"a polling session rewrote the row every read: {delivered}"
    )


@pytest.mark.asyncio
async def test_each_reader_is_recorded_separately(client, db_pool):
    """A shared address read by two sessions must show both — otherwise
    'delivered' cannot answer 'did ANYONE get this'."""
    addr = f"deliv-multi-{uuid.uuid4().hex[:8]}"
    mid = await _send(client, addr)

    for who in ("alpha@macmini", "beta@macmini"):
        r = await client.post("/memory/inbox", json={
            "listen_set": [addr], "reader_identity": who,
            "unread_only": False, "limit": 10,
        })
        assert r.status_code == 200

    delivered = await _delivered_to(db_pool, mid)
    assert set(delivered) == {"alpha@macmini", "beta@macmini"}


@pytest.mark.asyncio
async def test_delivery_does_not_imply_ack(client, db_pool):
    """The two fields must stay independent. Collapsing them would recreate
    the confusion this exists to end."""
    addr = f"deliv-noack-{uuid.uuid4().hex[:8]}"
    mid = await _send(client, addr)
    await client.post("/memory/inbox", json={
        "listen_set": [addr], "reader_identity": f"{addr}@macmini", "limit": 10,
    })

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT metadata->'read_by' AS r FROM memories WHERE key=$1", mid)
    import json as _j
    raw = row["r"]
    read_by = _j.loads(raw) if isinstance(raw, str) else (list(raw) if raw else [])

    assert await _delivered_to(db_pool, mid), "delivered should be set"
    assert read_by == [], "reading an inbox must NOT silently count as an ack"
