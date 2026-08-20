import uuid
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


# --- AUDIT-2: token rotations must leave a mark -----------------------------
# "When did this token die" was the first question asked during the 2026-08-16
# rotated-credential incident, and the store could not answer it: principals
# had no updated_at and the CRUD endpoints wrote no audit rows. The only bound
# available was a comment in a keys file.

@pytest.mark.asyncio
async def test_token_regeneration_is_recorded_and_never_leaks_the_token(client, db_pool):
    rotor = f"audit2-rotor-{uuid.uuid4().hex[:8]}"
    r = await client.post("/admin/principals", json={
        "name": rotor, "type": "agent",
        "read_namespaces": ["fleet"], "write_namespaces": ["fleet"],
    })
    assert r.status_code == 200
    created_at = r.json()["principal"]["created_at"]

    r2 = await client.post(f"/admin/principals/{rotor}/token")
    assert r2.status_code == 200
    raw = r2.json()["raw_token"]

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT action, detail::text AS d FROM audit_log "
            "WHERE action = 'principal.token_regenerate'"
        )
    assert rows, "a token rotation left NO audit row — the AUDIT-2 defect"
    blob = " ".join(r["d"] for r in rows)
    assert rotor in blob
    # An audit row that leaks the credential is worse than no audit row.
    assert raw not in blob, "the raw token was written into the audit trail"

    # And the principal itself must now carry a rotation timestamp.
    r3 = await client.get(f"/admin/principals/{rotor}")
    assert r3.status_code == 200
    body = r3.json()
    assert body.get("updated_at"), "no updated_at after a rotation"
    assert body["updated_at"] >= created_at


@pytest.mark.asyncio
async def test_deactivation_is_recorded(client, db_pool):
    doomed = f"audit2-doomed-{uuid.uuid4().hex[:8]}"
    r0 = await client.post("/admin/principals", json={
        "name": doomed, "type": "agent",
        "read_namespaces": ["fleet"], "write_namespaces": ["fleet"],
    })
    r = await client.delete(f"/admin/principals/{doomed}")
    assert r.status_code == 200

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT detail::text AS d FROM audit_log "
            "WHERE action = 'principal.deactivate'"
        )
    assert any(doomed in r["d"] for r in rows), (
        "deactivating a principal left no audit row"
    )
