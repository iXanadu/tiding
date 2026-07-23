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
async def test_list_all_namespaces(client):
    """Listing without namespace returns memories across all namespaces."""
    resp = await client.get("/admin/memories")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["total"] >= 0


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
            # SEC-6: deleting is now opt-in, and a prefix ending at a separator
            # is "broad" so it must also be named.
            "dry_run": False,
            "i_understand_this_deletes": f"{NS}:bd-session/",
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
            ON CONFLICT (namespace, key, scope, user_id, project) DO UPDATE SET created_at = NOW() - INTERVAL '60 days'
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
            "dry_run": False,
            "i_understand_this_deletes": f"{NS}:bd-",
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


# --- SEC-6: the guards that would have prevented the 2026-07-23 data loss ----
#
# A caller sent {"key_prefix": "inbox/", "confirm": false} believing it was a
# dry run. No such field existed, pydantic ignored it, the delete ran, and 1733
# rows were destroyed with no backup. Each test below blocks one link in that
# chain.

@pytest.mark.asyncio
async def test_unknown_field_is_rejected_not_ignored(client):
    """The exact call that caused the incident must now 422.

    An endpoint that ACCEPTS an unknown safety flag is worse than one with no
    safety flag: it returns success and confirms the caller's false belief.
    """
    resp = await client.post("/admin/bulk-delete", json={
        "namespace": NS,
        "key_prefix": "sec6-",
        "confirm": False,          # <- never existed; was silently ignored
    })
    assert resp.status_code == 422
    assert "confirm" in resp.text


@pytest.mark.asyncio
async def test_default_is_dry_run_and_deletes_nothing(client):
    """Omitting dry_run must PREVIEW, not destroy. Safe is the default."""
    keys = ["sec6-a", "sec6-b"]
    await _seed(client, keys)
    try:
        resp = await client.post("/admin/bulk-delete", json={
            "namespace": NS, "key_prefix": "sec6-",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["dry_run"] is True
        assert data["deleted_count"] == 0
        assert data["matched_count"] == 2
        assert sorted(data["sample_keys"]) == ["sec6-a", "sec6-b"]
        # and the rows are still there
        resp = await client.get("/admin/memories", params={"namespace": NS, "key_prefix": "sec6-"})
        assert resp.json()["total"] == 2
    finally:
        await _cleanup(client, keys)


@pytest.mark.asyncio
async def test_broad_prefix_refused_without_acknowledgement(client):
    """A prefix naming a CLASS of keys must be stated, not stumbled into."""
    keys = ["sec6-a"]
    await _seed(client, keys)
    try:
        resp = await client.post("/admin/bulk-delete", json={
            "namespace": NS, "key_prefix": "sec6-", "dry_run": False,
        })
        assert resp.status_code == 422
        assert "i_understand_this_deletes" in resp.text
        resp = await client.get("/admin/memories", params={"namespace": NS, "key_prefix": "sec6-"})
        assert resp.json()["total"] == 1, "refusal must not have deleted anything"
    finally:
        await _cleanup(client, keys)


@pytest.mark.asyncio
async def test_wrong_acknowledgement_is_refused(client):
    keys = ["sec6-a"]
    await _seed(client, keys)
    try:
        resp = await client.post("/admin/bulk-delete", json={
            "namespace": NS, "key_prefix": "sec6-", "dry_run": False,
            "i_understand_this_deletes": "yes",
        })
        assert resp.status_code == 422
    finally:
        await _cleanup(client, keys)


@pytest.mark.asyncio
async def test_exact_key_needs_no_acknowledgement(client):
    """Narrow, specific deletes stay ergonomic — the gate targets blast radius,
    not deletion itself. Otherwise people route around it."""
    keys = ["sec6-a-specific-key-name"]
    await _seed(client, keys)
    resp = await client.post("/admin/bulk-delete", json={
        "namespace": NS, "key_prefix": "sec6-a-specific-key-name", "dry_run": False,
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted_count"] == 1


@pytest.mark.asyncio
async def test_dry_run_and_delete_share_one_predicate(client):
    """The preview must count exactly what the delete removes.

    A preview built from a different predicate than the delete is a preview
    that can lie — which is precisely how a match count got read as a preview
    and 1733 rows went away.
    """
    keys = ["sec6-x", "sec6-y", "sec6-z"]
    await _seed(client, keys)
    try:
        preview = await client.post("/admin/bulk-delete", json={
            "namespace": NS, "key_prefix": "sec6-",
        })
        matched = preview.json()["matched_count"]
        real = await client.post("/admin/bulk-delete", json={
            "namespace": NS, "key_prefix": "sec6-", "dry_run": False,
            "i_understand_this_deletes": f"{NS}:sec6-",
        })
        assert real.json()["deleted_count"] == matched == 3
    finally:
        await _cleanup(client, keys)


# --- /admin/cleanup ---

@pytest.mark.asyncio
async def test_cleanup_manual(client, db_pool):
    # Insert an already-expired memory
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO memories (namespace, key, value, scope, user_id, tags, tags_search, search_text, expires_at)
            VALUES ($1, $2, $3, $4, $5, '', '', '', NOW() - INTERVAL '1 day')
            ON CONFLICT (namespace, key, scope, user_id, project) DO UPDATE SET expires_at = NOW() - INTERVAL '1 day'
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


# --- /admin/memories search and date filters ---

@pytest.mark.asyncio
async def test_list_search_text(client):
    """Search filter matches against key and value text."""
    keys = ["search-findme-alpha"]
    await _seed(client, keys)
    try:
        resp = await client.get("/admin/memories", params={
            "namespace": NS,
            "search": "findme",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        assert any("findme" in item["key"] for item in data["items"])
    finally:
        await _cleanup(client, keys)


@pytest.mark.asyncio
async def test_list_date_range(client, db_pool):
    """Date range filters (created_after, created_before) work."""
    # Insert a memory with old created_at
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO memories (namespace, key, value, scope, user_id, tags, tags_search, search_text, created_at)
            VALUES ($1, $2, $3, $4, $5, '', '', '', '2020-01-15T00:00:00Z')
            ON CONFLICT (namespace, key, scope, user_id, project) DO UPDATE SET created_at = '2020-01-15T00:00:00Z'
            """,
            NS, "date-range-old", "old memory", "user", "default",
        )
    try:
        # Should find it with a range that includes 2020-01
        resp = await client.get("/admin/memories", params={
            "namespace": NS,
            "created_after": "2020-01-01T00:00:00Z",
            "created_before": "2020-02-01T00:00:00Z",
        })
        data = resp.json()
        assert data["total"] >= 1
        assert any(item["key"] == "date-range-old" for item in data["items"])

        # Should NOT find it with a range that excludes 2020-01
        resp = await client.get("/admin/memories", params={
            "namespace": NS,
            "created_after": "2021-01-01T00:00:00Z",
        })
        data = resp.json()
        assert all(item["key"] != "date-range-old" for item in data["items"])
    finally:
        await _cleanup(client, ["date-range-old"])


# --- PATCH /admin/memories (update) ---

@pytest.mark.asyncio
async def test_update_memory_scope(client):
    """Update a memory's scope via PATCH."""
    keys = ["update-scope-test"]
    await _seed(client, keys, scope="user")
    try:
        resp = await client.patch("/admin/memories", json={
            "namespace": NS,
            "key": "update-scope-test",
            "scope": "user",
            "user_id": "default",
            "new_scope": "shared",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        # Verify scope changed
        resp = await client.get("/admin/memories", params={
            "namespace": NS,
            "key_prefix": "update-scope-test",
            "scope": "shared",
        })
        assert resp.json()["total"] == 1
    finally:
        await _cleanup(client, keys, scope="shared")


@pytest.mark.asyncio
async def test_update_memory_namespace(client):
    """Update a memory's namespace via PATCH."""
    keys = ["update-ns-test"]
    await _seed(client, keys)
    try:
        resp = await client.patch("/admin/memories", json={
            "namespace": NS,
            "key": "update-ns-test",
            "scope": "user",
            "user_id": "default",
            "new_namespace": "test-admin-moved",
        })
        assert resp.status_code == 200

        # Verify it moved
        resp = await client.get("/admin/memories", params={
            "namespace": "test-admin-moved",
            "key_prefix": "update-ns-test",
        })
        assert resp.json()["total"] == 1
    finally:
        await _cleanup(client, keys, ns="test-admin-moved")


@pytest.mark.asyncio
async def test_update_memory_not_found(client):
    """PATCH returns 404 for non-existent memory."""
    resp = await client.patch("/admin/memories", json={
        "namespace": NS,
        "key": "does-not-exist-xyz",
        "scope": "user",
        "user_id": "default",
        "new_scope": "shared",
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_memory_no_changes(client):
    """PATCH with no new_* fields returns 404 (no changes)."""
    keys = ["update-noop-test"]
    await _seed(client, keys)
    try:
        resp = await client.patch("/admin/memories", json={
            "namespace": NS,
            "key": "update-noop-test",
            "scope": "user",
            "user_id": "default",
        })
        assert resp.status_code == 404
    finally:
        await _cleanup(client, keys)


@pytest.mark.asyncio
async def test_list_returns_namespace_field(client):
    """MemoryListItem now includes namespace in response."""
    keys = ["ns-field-test"]
    await _seed(client, keys)
    try:
        resp = await client.get("/admin/memories", params={
            "namespace": NS,
            "key_prefix": "ns-field-test",
        })
        data = resp.json()
        assert data["total"] >= 1
        assert data["items"][0]["namespace"] == NS
    finally:
        await _cleanup(client, keys)


# --- metadata ---

@pytest.mark.asyncio
async def test_metadata_from_machine_header(client):
    """X-Engram-Machine header populates metadata.machine on stored memory."""
    key = "meta-machine-test"
    resp = await client.post(
        "/memory/set",
        json={
            "namespace": NS,
            "key": key,
            "value": "testing metadata",
            "expiration_days": 0,
        },
        headers={"X-Engram-Machine": "testbox"},
    )
    assert resp.status_code == 200
    try:
        resp = await client.get("/admin/memories", params={
            "namespace": NS,
            "key_prefix": key,
        })
        data = resp.json()
        assert data["total"] >= 1
        item = data["items"][0]
        assert item["metadata"] is not None
        assert item["metadata"]["machine"] == "testbox"
    finally:
        await _cleanup(client, [key])


@pytest.mark.asyncio
async def test_metadata_null_when_no_header(client):
    """Without X-Engram-Machine header or principal, metadata is null."""
    key = "meta-null-test"
    resp = await client.post("/memory/set", json={
        "namespace": NS,
        "key": key,
        "value": "no metadata",
        "expiration_days": 0,
    })
    assert resp.status_code == 200
    try:
        resp = await client.get("/admin/memories", params={
            "namespace": NS,
            "key_prefix": key,
        })
        data = resp.json()
        assert data["total"] >= 1
        assert data["items"][0]["metadata"] is None
    finally:
        await _cleanup(client, [key])


@pytest.mark.asyncio
async def test_metadata_survives_upsert(client):
    """On upsert, metadata is updated to the new value."""
    key = "meta-upsert-test"
    # First write with machine=box1
    await client.post(
        "/memory/set",
        json={"namespace": NS, "key": key, "value": "v1", "expiration_days": 0},
        headers={"X-Engram-Machine": "box1"},
    )
    # Upsert with machine=box2
    await client.post(
        "/memory/set",
        json={"namespace": NS, "key": key, "value": "v2", "expiration_days": 0},
        headers={"X-Engram-Machine": "box2"},
    )
    try:
        resp = await client.get("/admin/memories", params={
            "namespace": NS,
            "key_prefix": key,
        })
        data = resp.json()
        item = data["items"][0]
        assert item["metadata"]["machine"] == "box2"
    finally:
        await _cleanup(client, [key])
