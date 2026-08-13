"""MEM-6: project memory belongs to the PROJECT; the writer is provenance.

Owner directive 2026-08-13 ("we can NOT pin a project state, or corporate
memory, to an AGENT that may never run again"), measured the same day: seven
projects held split memory across two writer principals, five of them on
exactly the handoff keys (startup/next, wip/current) — two providers handing
off to themselves in parallel, neither able to see the other.

Two moves that compose (decision/mem-6-design-write-supersedes-read-collapses):
- WRITE auto-supersedes any other writer's live twin (server-side, all
  clients inherit; the twin is kept — value and writer untouched — it merely
  drains from default reads, per the ec6518a supersede lifecycle).
- READ collapses via user_id="*" on memory_get, scope=project only,
  mirroring the MEM-5 search rule (grants nothing new: namespace still
  gates, any writer's row was always readable by naming the writer).
"""

import pytest

PROJ = "mem6test"
NS = "fleet"


async def _set(client, user_id, value, key="mem6/handoff", **kw):
    resp = await client.post("/memory/set", json={
        "namespace": NS, "key": key, "value": value,
        "scope": "project", "user_id": user_id, "project": PROJ, **kw,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _get(client, user_id, key="mem6/handoff"):
    resp = await client.post("/memory/get", json={
        "namespace": NS, "key": key,
        "scope": "project", "user_id": user_id, "project": PROJ,
    })
    return resp.json()


async def _cleanup(client, db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE project = $1", PROJ
        )


@pytest.mark.asyncio
async def test_wildcard_get_reads_another_writers_row(client, db_pool):
    """The MEM-6 core: an exact-key read spans writers with user_id='*'.

    This is the handoff shape — grok wrote startup/next, claude reads it.
    Before this, the read returned not_found and the reader could not tell
    "no handoff exists" from "the handoff is in a partition I didn't name".
    """
    try:
        await _set(client, "grok", "grok's handoff note")
        got = await _get(client, "*")
        assert got["status"] == "ok", got
        assert got["memory"]["value"] == "grok's handoff note"
        # Provenance survives the collapse — the reader can see WHO wrote it.
        assert got["memory"]["user_id"] == "grok"
    finally:
        await _cleanup(client, db_pool)


@pytest.mark.asyncio
async def test_a_project_write_supersedes_the_other_writers_twin(
    client, db_pool
):
    """Latest write wins ACROSS writers — which is what a handoff key means.

    claude writing after grok must not create a second live row that ranks
    beside the first with nothing marking which is current (the measured
    MEM-3 residual). The twin is superseded, not deleted: value and writer
    stay retrievable, so provenance and history survive.
    """
    try:
        await _set(client, "grok", "stale handoff")
        await _set(client, "claude-code", "current handoff")

        # The collapse returns the newer write, whoever asks.
        got = await _get(client, "*")
        assert got["memory"]["value"] == "current handoff"
        assert got["memory"]["user_id"] == "claude-code"

        # The twin is KEPT, marked superseded — corrected, not erased.
        async with db_pool.acquire() as conn:
            twin = await conn.fetchrow(
                """
                SELECT value, metadata->>'status' AS status,
                       metadata->>'superseded_by_user_id' AS by_user
                FROM memories
                WHERE project = $1 AND key = 'mem6/handoff'
                  AND user_id = 'grok'
                """,
                PROJ,
            )
        assert twin is not None, "the superseded twin must not be deleted"
        assert twin["value"] == "stale handoff", "history stays verbatim"
        assert twin["status"] == "superseded"
        assert twin["by_user"] == "claude-code", (
            "the stamp must name the replacing writer"
        )
    finally:
        await _cleanup(client, db_pool)


@pytest.mark.asyncio
async def test_alternating_writers_converge_on_the_latest(client, db_pool):
    """A second write by the ORIGINAL writer takes the key back.

    grok → claude → grok again: each write supersedes the other's twin and
    revives nothing; the wildcard read always answers with the last write.
    This is the organic healing the design relies on instead of a backfill
    sweep — the measured split keys are rewritten every session.
    """
    try:
        await _set(client, "grok", "v1 by grok")
        await _set(client, "claude-code", "v2 by claude")
        await _set(client, "grok", "v3 by grok")

        got = await _get(client, "*")
        assert got["memory"]["value"] == "v3 by grok"
        assert got["memory"]["user_id"] == "grok"

        # Exactly ONE live row remains; everything else is superseded.
        async with db_pool.acquire() as conn:
            live = await conn.fetch(
                """
                SELECT user_id FROM memories
                WHERE project = $1 AND key = 'mem6/handoff'
                  AND COALESCE(metadata->>'status', '') <> 'superseded'
                """,
                PROJ,
            )
        assert [r["user_id"] for r in live] == ["grok"], (
            f"expected one live row (grok's), world: {live}"
        )
    finally:
        await _cleanup(client, db_pool)


@pytest.mark.asyncio
async def test_wildcard_is_a_literal_outside_project_scope(client, db_pool):
    """For scope=user/machine, user_id='*' matches nothing but itself.

    There user_id is a PERSON or a HOST; spanning it would be a disclosure,
    not a fix. Same boundary the MEM-5 search rule drew.
    """
    try:
        resp = await client.post("/memory/set", json={
            "namespace": NS, "key": "mem6/private", "value": "personal",
            "scope": "user", "user_id": "somebody",
        })
        assert resp.status_code == 200
        got = await client.post("/memory/get", json={
            "namespace": NS, "key": "mem6/private",
            "scope": "user", "user_id": "*",
        })
        assert got.json()["status"] == "not_found", (
            "wildcard must not span user-scope partitions"
        )
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM memories WHERE key = 'mem6/private'"
            )


@pytest.mark.asyncio
async def test_shared_scope_writes_do_not_supersede_anyone(client, db_pool):
    """The auto-supersede is scope=project ONLY.

    Shared scope has always been a common namespace where same-key rows
    under different user_ids are separate facts, not competing versions of
    one logical key — a write there must not silently retire a stranger's
    row.
    """
    try:
        for uid, val in [("global", "the shared fact"),
                         ("other-writer", "a different shared fact")]:
            resp = await client.post("/memory/set", json={
                "namespace": NS, "key": "mem6/shared-fact", "value": val,
                "scope": "shared", "user_id": uid,
            })
            assert resp.status_code == 200
        async with db_pool.acquire() as conn:
            live = await conn.fetch(
                """
                SELECT user_id FROM memories
                WHERE key = 'mem6/shared-fact'
                  AND COALESCE(metadata->>'status', '') <> 'superseded'
                """,
            )
        assert len(live) == 2, (
            f"shared-scope twins must BOTH stay live, world: {live}"
        )
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM memories WHERE key = 'mem6/shared-fact'"
            )


@pytest.mark.asyncio
async def test_wildcard_get_skips_a_superseded_row_with_no_live_twin(
    client, db_pool
):
    """A key whose ONLY row is superseded reads as not_found via '*'.

    Superseded means "drained from default reads"; the wildcard must not
    resurrect it just because nothing newer exists — that would re-open the
    MEM-3 incident (a stale note out-ranking its own correction) through the
    exact-key door.
    """
    try:
        await _set(client, "grok", "retired note", key="mem6/retired")
        r = await client.post("/memory/supersede", json={
            "namespace": NS, "key": "mem6/retired", "project": PROJ,
            "target_user_id": "grok", "reason": "test: retired with no successor",
        })
        assert r.status_code == 200, r.text
        got = await _get(client, "*", key="mem6/retired")
        assert got["status"] == "not_found", (
            "a superseded row must stay drained even when nothing replaced it"
        )
    finally:
        await _cleanup(client, db_pool)
