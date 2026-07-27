"""AUDIT-1: the write trail actually gets written.

The audit_log table shipped with the principals work and sat at zero rows —
"who wrote what, when" was unanswerable, proven costly twice in one week
(an undatable /memory/forget during a data-loss incident; three inferential
queries to establish whether a shut-down agent had stored its findings).
These tests pin the writers, not the table.
"""

import json

import pytest

NS = "audittest"


async def _rows(db_pool, action):
    async with db_pool.acquire() as conn:
        return [
            (r["action"], json.loads(r["detail"]))
            for r in await conn.fetch(
                "SELECT action, detail FROM audit_log WHERE action = $1 "
                "AND detail::jsonb ->> 'namespace' = $2 ORDER BY created_at",
                action, NS,
            )
        ]


async def _clear(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM audit_log WHERE detail::jsonb ->> 'namespace' = $1", NS)
        await conn.execute("DELETE FROM memories WHERE namespace = $1", NS)


@pytest.mark.asyncio
async def test_set_and_overwrite_both_leave_a_trail(client, db_pool):
    """The overwrite is the row that matters most: "created": false is the
    write no backup window could reconstruct (the 2026-07-25 race resolved
    inside one 30-minute dump interval — only a trail can see it)."""
    await _clear(db_pool)
    r = await client.post("/memory/set", json={
        "namespace": NS, "key": "trail/a", "value": "first"})
    assert r.status_code == 200
    r = await client.post("/memory/set", json={
        "namespace": NS, "key": "trail/a", "value": "second"})
    assert r.status_code == 200

    rows = await _rows(db_pool, "memory.set")
    assert len(rows) == 2, "every write must leave a row"
    assert rows[0][1]["created"] is True
    assert rows[1][1]["created"] is False, (
        "the overwrite was not distinguishable from a create — the exact "
        "question the 2026-07-24 forensics could not answer"
    )
    assert rows[0][1]["key"] == "trail/a"
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_forget_records_hits_and_misses(client, db_pool):
    await _clear(db_pool)
    await client.post("/memory/set", json={
        "namespace": NS, "key": "trail/b", "value": "doomed"})
    r = await client.post("/memory/forget", json={
        "namespace": NS, "key": "trail/b"})
    assert r.json()["status"] == "ok"
    r = await client.post("/memory/forget", json={
        "namespace": NS, "key": "trail/never-existed"})
    assert r.json()["status"] == "not_found"

    rows = await _rows(db_pool, "memory.forget")
    assert len(rows) == 2
    assert rows[0][1] == {**rows[0][1], "key": "trail/b", "deleted": True}
    assert rows[1][1]["deleted"] is False, (
        "an attempted delete of an absent key is forensically interesting "
        "and must be recorded, not skipped"
    )
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_executed_bulk_delete_is_recorded_with_predicate_and_count(
    client, db_pool
):
    await _clear(db_pool)
    for i in range(3):
        await client.post("/memory/set", json={
            "namespace": NS, "key": f"bulk/{i}", "value": "x"})
    # dry run first — must NOT hit the trail (nothing was destroyed)
    r = await client.post("/admin/bulk-delete", json={
        "namespace": NS, "key_prefix": "bulk/", "dry_run": True})
    assert r.status_code == 200
    assert await _rows(db_pool, "admin.bulk_delete") == []

    r = await client.post("/admin/bulk-delete", json={
        "namespace": NS, "key_prefix": "bulk/", "dry_run": False,
        # trailing-slash prefix is a broad predicate; name the blast radius
        "i_understand_this_deletes": f"{NS}:bulk/"})
    assert r.status_code == 200
    rows = await _rows(db_pool, "admin.bulk_delete")
    assert len(rows) == 1
    detail = rows[0][1]
    assert detail["key_prefix"] == "bulk/"
    assert detail["deleted"] == 3, "the count is the blast radius — record it"
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_audit_failure_never_fails_the_write(client, db_pool, monkeypatch):
    """Best-effort by construction: a store that refuses writes because its
    bookkeeping is down has inverted its priorities."""
    from server.services import audit_service

    async def _broken(*a, **k):
        raise RuntimeError("audit backend down")

    monkeypatch.setattr(audit_service, "get_pool", _broken)
    r = await client.post("/memory/set", json={
        "namespace": NS, "key": "trail/survives", "value": "still lands"})
    assert r.status_code == 200, (
        "the memory write must succeed even when the audit insert cannot"
    )
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE namespace = $1", NS)
