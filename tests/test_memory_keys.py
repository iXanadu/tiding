"""MEM-2: deterministic key enumeration — the verb between get and search.

The claim under test: `/memory/keys` returns EVERY key under a prefix, in key
order, with no embedding involved, and an empty answer proves absence in the
partition it names. Semantic search cannot do this — eight differently-phrased
searches returning nothing is evidence, not proof, and the live incident that
justified the verb ("an agent was shut down mid-job: did it store anything?")
took direct SQL to answer.
"""

import pytest

NS = "keystest"


async def _put(client, key, value="v", scope="machine", user_id="host1", **kw):
    body = {
        "namespace": NS, "key": key, "value": value,
        "scope": scope, "user_id": user_id,
    }
    body.update(kw)
    r = await client.post("/memory/set", json=body)
    assert r.status_code == 200, r.text


async def _clear(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE namespace = $1", NS)


@pytest.mark.asyncio
async def test_prefix_listing_is_complete_and_key_ordered(client, db_pool):
    await _clear(db_pool)
    for key in ("wip/beta", "wip/alpha", "wip/gamma", "fix/other", "wip"):
        await _put(client, key)

    r = await client.post("/memory/keys", json={
        "namespace": NS, "prefix": "wip/", "scope": "machine",
        "user_id": "host1",
    })
    assert r.status_code == 200
    data = r.json()
    assert [k["key"] for k in data["keys"]] == [
        "wip/alpha", "wip/beta", "wip/gamma",
    ]
    assert data["total"] == 3
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_empty_prefix_lists_the_whole_partition(client, db_pool):
    """The absence check: 'did that agent store ANYTHING?' is prefix=''. """
    await _clear(db_pool)
    for key in ("a/one", "b/two"):
        await _put(client, key)

    r = await client.post("/memory/keys", json={
        "namespace": NS, "prefix": "", "scope": "machine", "user_id": "host1",
    })
    assert r.json()["total"] == 2

    nothing = await client.post("/memory/keys", json={
        "namespace": NS, "prefix": "", "scope": "machine",
        "user_id": "nobody-ever-wrote-here",
    })
    assert nothing.json()["total"] == 0
    assert nothing.json()["keys"] == []
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_prefix_is_a_literal_not_a_like_pattern(client, db_pool):
    """'wip_' must mean those four characters — LIKE metacharacters in a
    prefix would silently widen the match, and a listing that includes keys
    outside the asked-for prefix misreports what exists."""
    await _clear(db_pool)
    await _put(client, "wip_x")
    await _put(client, "wipZx")  # would match 'wip_' as a raw LIKE pattern

    r = await client.post("/memory/keys", json={
        "namespace": NS, "prefix": "wip_", "scope": "machine",
        "user_id": "host1",
    })
    assert [k["key"] for k in r.json()["keys"]] == ["wip_x"]
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_truncation_is_legible_via_total(client, db_pool):
    """A capped enumeration that looks complete would be the exact failure
    this verb exists to end — `total` must carry the full count."""
    await _clear(db_pool)
    for i in range(5):
        await _put(client, f"many/{i}")

    r = await client.post("/memory/keys", json={
        "namespace": NS, "prefix": "many/", "scope": "machine",
        "user_id": "host1", "limit": 2,
    })
    data = r.json()
    assert len(data["keys"]) == 2
    assert data["total"] == 5
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_superseded_rows_are_listed_and_marked(client, db_pool):
    """A census that hides corrected rows cannot prove a write happened."""
    await _clear(db_pool)
    await _put(client, "note/thing", value="old", scope="project",
               user_id="writer1", project="projk")
    r = await client.post("/memory/supersede", json={
        "namespace": NS, "key": "note/thing", "project": "projk",
        "target_user_id": "writer1", "reason": "corrected by test",
    })
    assert r.status_code == 200, r.text

    listing = (await client.post("/memory/keys", json={
        "namespace": NS, "prefix": "note/", "scope": "project",
        "user_id": "*", "project": "projk",
    })).json()
    statuses = {k["status"] for k in listing["keys"]}
    assert "superseded" in statuses, (
        "the corrected row must still be listed, marked"
    )
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_listing_serves_the_index_not_the_content(client, db_pool):
    """Enumeration answers 'what exists'; memory_get answers 'what does it
    say'. Values must not ride along — only their length."""
    await _clear(db_pool)
    await _put(client, "big/one", value="x" * 1000)

    entry = (await client.post("/memory/keys", json={
        "namespace": NS, "prefix": "big/", "scope": "machine",
        "user_id": "host1",
    })).json()["keys"][0]
    assert entry["value_chars"] == 1000
    assert "value" not in entry
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_project_scope_spans_writers_with_wildcard(client, db_pool):
    """MEM-5 parity with search: a project's memory belongs to the PROJECT.
    user_id='*' spans writers for scope=project only."""
    await _clear(db_pool)
    await _put(client, "shared/a", scope="project", user_id="writer1",
               project="projk")
    await _put(client, "shared/b", scope="project", user_id="writer2",
               project="projk")

    spanned = (await client.post("/memory/keys", json={
        "namespace": NS, "prefix": "shared/", "scope": "project",
        "user_id": "*", "project": "projk",
    })).json()
    assert spanned["total"] == 2

    pinned = (await client.post("/memory/keys", json={
        "namespace": NS, "prefix": "shared/", "scope": "project",
        "user_id": "writer1", "project": "projk",
    })).json()
    assert spanned["total"] == 2 and pinned["total"] == 1
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_wildcard_is_a_literal_outside_project_scope(client, db_pool):
    """For scope=machine user_id names a HOST — spanning it would be a
    disclosure, not a fix. Same rule as search."""
    await _clear(db_pool)
    await _put(client, "m/one", scope="machine", user_id="hostA")
    await _put(client, "m/two", scope="machine", user_id="hostB")

    r = (await client.post("/memory/keys", json={
        "namespace": NS, "prefix": "m/", "scope": "machine", "user_id": "*",
    })).json()
    assert r["total"] == 0  # no host is literally named '*'
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_inbox_rows_are_never_enumerable(client, db_pool):
    """Mail has its own lifecycle surface; the generic read path must not
    reach it — parity with search's guard."""
    r = await client.post("/memory/keys", json={
        "namespace": NS, "prefix": "", "scope": "inbox", "user_id": "anyone",
    })
    assert r.status_code == 200
    assert r.json()["total"] == 0
