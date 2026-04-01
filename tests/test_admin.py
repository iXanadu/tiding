"""Integration tests for admin endpoints."""

import pytest

NS = "test-admin"


async def _seed(client, keys, scope="user", user_id="default", ns=NS):
    """Seed memories and return list of keys created."""
    for key in keys:
        resp = await client.post("/memory/set", json={
            "namespace": ns,
            "key": key,
            "value": f"value for {key}",
            "scope": scope,
            "user_id": user_id,
            "tags": "admin-test",
            "expiration_days": 0,  # never expires
        })
        assert resp.status_code == 200


async def _cleanup(client, keys, scope="user", user_id="default", ns=NS):
    for key in keys:
        await client.post("/memory/forget", json={
            "namespace": ns,
            "key": key,
            "scope": scope,
            "user_id": user_id,
        })


# --- /admin/memories ---

@pytest.mark.asyncio
async def test_list_basic(client):
    keys = [f"list-basic-{i}" for i in range(5)]
    await _seed(client, keys)
    try:
        resp = await client.get("/admin/memories", params={"namespace": NS})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["total"] >= 5
        # values not included by default
        for item in data["items"]:
            assert item["value"] is None
    finally:
        await _cleanup(client, keys)


@pytest.mark.asyncio
async def test_list_with_value(client):
    keys = ["list-val-1"]
    await _seed(client, keys)
    try:
        resp = await client.get("/admin/memories", params={
            "namespace": NS,
            "include_value": "true",
            "value_max_length": 10,
        })
        assert resp.status_code == 200
        data = resp.json()
        found = [i for i in data["items"] if i["key"] == "list-val-1"]
        assert len(found) == 1
        assert found[0]["value"] is not None
        assert len(found[0]["value"]) <= 10
    finally:
        await _cleanup(client, keys)


@pytest.mark.asyncio
async def test_list_key_prefix(client):
    keys = ["prefix-a-1", "prefix-a-2", "prefix-b-1"]
    await _seed(client, keys)
    try:
        resp = await client.get("/admin/memories", params={
            "namespace": NS,
            "key_prefix": "prefix-a",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        result_keys = {i["key"] for i in data["items"]}
        assert result_keys == {"prefix-a-1", "prefix-a-2"}
    finally:
        await _cleanup(client, keys)


@pytest.mark.asyncio
async def test_list_scope_filter(client):
    keys_shared = ["scope-shared-1"]
    keys_machine = ["scope-machine-1"]
    await _seed(client, keys_shared, scope="shared")
    await _seed(client, keys_machine, scope="machine")
    try:
        resp = await client.get("/admin/memories", params={
            "namespace": NS,
            "scope": "shared",
        })
        data = resp.json()
        result_keys = {i["key"] for i in data["items"]}
        assert "scope-shared-1" in result_keys
        assert "scope-machine-1" not in result_keys
    finally:
        await _cleanup(client, keys_shared, scope="shared")
        await _cleanup(client, keys_machine, scope="machine")


@pytest.mark.asyncio
async def test_list_pagination(client):
    keys = [f"page-{i:02d}" for i in range(5)]
    await _seed(client, keys)
    try:
        resp = await client.get("/admin/memories", params={
            "namespace": NS,
            "key_prefix": "page-",
            "limit": 2,
            "offset": 0,
            "sort_by": "key",
            "sort_order": "asc",
        })
        data = resp.json()
        assert data["total"] == 5
        assert len(data["items"]) == 2
        assert data["items"][0]["key"] == "page-00"
        assert data["items"][1]["key"] == "page-01"

        # Page 2
        resp = await client.get("/admin/memories", params={
            "namespace": NS,
            "key_prefix": "page-",
            "limit": 2,
            "offset": 2,
            "sort_by": "key",
            "sort_order": "asc",
        })
        data = resp.json()
        assert data["items"][0]["key"] == "page-02"
    finally:
        await _cleanup(client, keys)


@pytest.mark.asyncio
async def test_list_sort(client):
    keys = ["sort-a", "sort-b", "sort-c"]
    await _seed(client, keys)
    try:
        resp = await client.get("/admin/memories", params={
            "namespace": NS,
            "key_prefix": "sort-",
            "sort_by": "key",
            "sort_order": "desc",
        })
        data = resp.json()
        result_keys = [i["key"] for i in data["items"]]
        assert result_keys == ["sort-c", "sort-b", "sort-a"]
    finally:
        await _cleanup(client, keys)


@pytest.mark.asyncio
async def test_list_namespace_required(client):
    resp = await client.get("/admin/memories")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_cross_namespace(client):
    """List memories across multiple namespaces using comma-separated param."""
    ns_a = "test-cross-a"
    ns_b = "test-cross-b"
    keys_a = ["cross-list-1"]
    keys_b = ["cross-list-2"]
    await _seed(client, keys_a, ns=ns_a)
    await _seed(client, keys_b, ns=ns_b)
    try:
        resp = await client.get("/admin/memories", params={
            "namespace": f"{ns_a},{ns_b}",
        })
        assert resp.status_code == 200
        data = resp.json()
        result_keys = {i["key"] for i in data["items"]}
        result_ns = {i["namespace"] for i in data["items"]}
        assert "cross-list-1" in result_keys
        assert "cross-list-2" in result_keys
        assert ns_a in result_ns
        assert ns_b in result_ns
    finally:
        await _cleanup(client, keys_a, ns=ns_a)
        await _cleanup(client, keys_b, ns=ns_b)


# --- /admin/stats ---

@pytest.mark.asyncio
async def test_stats_all(client):
    keys_a = ["stats-a-1"]
    keys_b = ["stats-b-1"]
    await _seed(client, keys_a, ns="test-stats-a")
    await _seed(client, keys_b, ns="test-stats-b")
    try:
        resp = await client.get("/admin/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        ns_names = {s["namespace"] for s in data["stats"]}
        assert "test-stats-a" in ns_names
        assert "test-stats-b" in ns_names
        for s in data["stats"]:
            assert s["count"] > 0
    finally:
        await _cleanup(client, keys_a, ns="test-stats-a")
        await _cleanup(client, keys_b, ns="test-stats-b")


@pytest.mark.asyncio
async def test_stats_by_scope(client):
    keys = ["stats-scope-1"]
    await _seed(client, keys, scope="shared")
    try:
        resp = await client.get("/admin/stats", params={
            "namespace": NS,
            "by_scope": "true",
        })
        data = resp.json()
        assert data["status"] == "ok"
        for s in data["stats"]:
            assert s["scope"] is not None
    finally:
        await _cleanup(client, keys, scope="shared")


# --- /admin/bulk-delete ---

@pytest.mark.asyncio
async def test_bulk_delete_by_prefix(client):
    keys = ["bd-session/a", "bd-session/b", "bd-keep-me"]
    await _seed(client, keys)
    try:
        resp = await client.post("/admin/bulk-delete", json={
            "namespace": NS,
            "key_prefix": "bd-session/",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["deleted_count"] == 2

        # Verify keep-me survived
        resp = await client.post("/memory/get", json={
            "namespace": NS,
            "key": "bd-keep-me",
        })
        assert resp.json()["status"] == "ok"
    finally:
        await _cleanup(client, ["bd-keep-me"])


@pytest.mark.asyncio
async def test_bulk_delete_older_than(client, db_pool):
    # Insert one memory with old created_at directly via SQL
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO memories (namespace, key, value, scope, user_id, tags, tags_search, search_text, created_at)
            VALUES ($1, $2, $3, $4, $5, '', '', '', NOW() - INTERVAL '60 days')
            ON CONFLICT (namespace, key, scope, user_id) DO UPDATE SET created_at = NOW() - INTERVAL '60 days'
            """,
            NS, "bd-old-row", "old value", "user", "default",
        )

    keys_new = ["bd-new-row"]
    await _seed(client, keys_new)
    try:
        resp = await client.post("/admin/bulk-delete", json={
            "namespace": NS,
            "key_prefix": "bd-",
            "older_than_days": 30,
        })
        data = resp.json()
        assert data["status"] == "ok"
        assert data["deleted_count"] == 1  # only the old row

        # New row still exists
        resp = await client.get("/admin/memories", params={
            "namespace": NS,
            "key_prefix": "bd-new-row",
        })
        assert resp.json()["total"] == 1
    finally:
        await _cleanup(client, keys_new)
        # Clean up old row if still present
        await _cleanup(client, ["bd-old-row"])


@pytest.mark.asyncio
async def test_bulk_delete_empty_prefix(client):
    resp = await client.post("/admin/bulk-delete", json={
        "namespace": NS,
        "key_prefix": "",
    })
    assert resp.status_code == 422


# --- /admin/cleanup ---

@pytest.mark.asyncio
async def test_cleanup_manual(client, db_pool):
    # Insert an already-expired memory
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO memories (namespace, key, value, scope, user_id, tags, tags_search, search_text, expires_at)
            VALUES ($1, $2, $3, $4, $5, '', '', '', NOW() - INTERVAL '1 day')
            ON CONFLICT (namespace, key, scope, user_id) DO UPDATE SET expires_at = NOW() - INTERVAL '1 day'
            """,
            NS, "cleanup-expired", "should be removed", "user", "default",
        )

    resp = await client.post("/admin/cleanup")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["deleted_count"] >= 1

    # Verify it's gone
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM memories WHERE namespace = $1 AND key = $2",
            NS, "cleanup-expired",
        )
        assert row is None


@pytest.mark.asyncio
async def test_cleanup_nothing(client):
    resp = await client.post("/admin/cleanup")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    # deleted_count should be 0 or small (only previously expired rows)
    assert isinstance(data["deleted_count"], int)
