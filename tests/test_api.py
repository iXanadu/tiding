"""Integration tests for the HTTP API endpoints."""

import pytest


@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("ok", "degraded")
    assert "checks" in data


@pytest.mark.asyncio
async def test_set_and_get(client):
    # Set
    resp = await client.post("/memory/set", json={
        "namespace": "test",
        "key": "test_api_key",
        "value": "test_api_value",
        "tags": "testing api",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    # Get
    resp = await client.post("/memory/get", json={
        "namespace": "test",
        "key": "test_api_key",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["memory"]["value"] == "test_api_value"
    assert data["memory"]["namespace"] == "test"
    # created_at must surface so readers can date what they recite
    assert data["memory"]["created_at"] is not None

    # Clean up
    await client.post("/memory/forget", json={
        "namespace": "test",
        "key": "test_api_key",
    })


@pytest.mark.asyncio
async def test_search(client):
    # Store a memory
    await client.post("/memory/set", json={
        "namespace": "test",
        "key": "favorite_color",
        "value": "blue",
        "tags": "preference color",
    })

    # Search semantically
    resp = await client.post("/memory/search", json={
        "namespace": "test",
        "query": "what color do they like",
        "limit": 3,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert len(data["results"]) > 0
    assert any(r["key"] == "favorite_color" for r in data["results"])
    # every search result carries created_at for recency-aware reading
    assert all(r["created_at"] is not None for r in data["results"])

    # Clean up
    await client.post("/memory/forget", json={
        "namespace": "test",
        "key": "favorite_color",
    })


@pytest.mark.asyncio
async def test_forget(client):
    await client.post("/memory/set", json={
        "namespace": "test",
        "key": "to_delete",
        "value": "gone",
    })
    resp = await client.post("/memory/forget", json={
        "namespace": "test",
        "key": "to_delete",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    resp = await client.post("/memory/get", json={
        "namespace": "test",
        "key": "to_delete",
    })
    assert resp.json()["status"] == "not_found"


@pytest.mark.asyncio
async def test_get_not_found(client):
    resp = await client.post("/memory/get", json={
        "namespace": "test",
        "key": "nonexistent_key_12345",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "not_found"


@pytest.mark.asyncio
async def test_search_empty_query_rejected(client):
    resp = await client.post("/memory/search", json={
        "namespace": "test",
        "query": "",
    })
    assert resp.status_code == 422

    resp = await client.post("/memory/search", json={
        "namespace": "test",
        "query": "   ",
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_namespace_isolation(client):
    """Same key in two namespaces should be independent."""
    # Store in namespace "alpha"
    await client.post("/memory/set", json={
        "namespace": "alpha",
        "key": "shared_key",
        "value": "alpha_value",
    })
    # Store in namespace "beta"
    await client.post("/memory/set", json={
        "namespace": "beta",
        "key": "shared_key",
        "value": "beta_value",
    })

    # Get from alpha
    resp = await client.post("/memory/get", json={
        "namespace": "alpha",
        "key": "shared_key",
    })
    assert resp.json()["memory"]["value"] == "alpha_value"

    # Get from beta
    resp = await client.post("/memory/get", json={
        "namespace": "beta",
        "key": "shared_key",
    })
    assert resp.json()["memory"]["value"] == "beta_value"

    # Clean up
    await client.post("/memory/forget", json={"namespace": "alpha", "key": "shared_key"})
    await client.post("/memory/forget", json={"namespace": "beta", "key": "shared_key"})


@pytest.mark.asyncio
async def test_namespace_required(client):
    """Omitting namespace returns 422 for set/get/forget (schema-required).

    Search is special: omitting namespace is allowed when the caller has
    a principal — the server resolves to read_namespaces. Without a
    principal, search returns 401 (auth required to resolve)."""
    resp = await client.post("/memory/set", json={
        "key": "no_ns",
        "value": "should fail",
    })
    assert resp.status_code == 422

    resp = await client.post("/memory/get", json={"key": "no_ns"})
    assert resp.status_code == 422

    # Anonymous caller with no namespace → 401 (server can't resolve without identity)
    resp = await client.post("/memory/search", json={"query": "anything"})
    assert resp.status_code == 401

    resp = await client.post("/memory/forget", json={"key": "no_ns"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_cross_namespace_search(client):
    """Search across multiple namespaces using the 'namespaces' field."""
    # Store in two different namespaces
    await client.post("/memory/set", json={
        "namespace": "ns-alpha",
        "key": "cross-ns-color",
        "value": "the sky is blue",
        "tags": "color preference",
    })
    await client.post("/memory/set", json={
        "namespace": "ns-beta",
        "key": "cross-ns-food",
        "value": "pizza is the best food",
        "tags": "food preference",
    })

    try:
        # Search across both namespaces
        resp = await client.post("/memory/search", json={
            "namespaces": ["ns-alpha", "ns-beta"],
            "query": "what are the preferences",
            "limit": 10,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        result_ns = {r["namespace"] for r in data["results"]}
        result_keys = {r["key"] for r in data["results"]}
        assert "ns-alpha" in result_ns
        assert "ns-beta" in result_ns
        assert "cross-ns-color" in result_keys
        assert "cross-ns-food" in result_keys

        # Single namespace still works (backward compat)
        resp = await client.post("/memory/search", json={
            "namespace": "ns-alpha",
            "query": "what are the preferences",
            "limit": 10,
        })
        assert resp.status_code == 200
        data = resp.json()
        result_ns = {r["namespace"] for r in data["results"]}
        assert result_ns == {"ns-alpha"}
    finally:
        await client.post("/memory/forget", json={"namespace": "ns-alpha", "key": "cross-ns-color"})
        await client.post("/memory/forget", json={"namespace": "ns-beta", "key": "cross-ns-food"})


@pytest.mark.asyncio
async def test_search_without_namespace_requires_principal(client):
    """Omitting namespace is OK when a principal is attached — server resolves
    via read_namespaces. Without a principal we 401 because we can't resolve."""
    resp = await client.post("/memory/search", json={"query": "anything"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_search_resolves_namespaces_from_principal(client):
    """When the caller omits namespace and presents a principal, the server
    expands the search to the principal's read_namespaces."""
    # Set up a principal with explicit read list and seed memories in both.
    create = await client.post("/admin/principals", json={
        "name": "ns-resolve-agent",
        "type": "agent",
        "read_namespaces": ["ns-r-alpha", "ns-r-beta"],
        "write_namespaces": ["ns-r-alpha", "ns-r-beta"],
    })
    token = create.json()["raw_token"]
    headers = {"Authorization": f"Bearer {token}"}

    await client.post("/memory/set", headers=headers, json={
        "namespace": "ns-r-alpha",
        "key": "resolve-color",
        "value": "the sky is blue",
        "tags": "color preference",
    })
    await client.post("/memory/set", headers=headers, json={
        "namespace": "ns-r-beta",
        "key": "resolve-food",
        "value": "pizza is the best food",
        "tags": "food preference",
    })

    try:
        # No namespace in the request — server resolves both from the principal.
        resp = await client.post(
            "/memory/search",
            headers=headers,
            json={"query": "what are the preferences", "limit": 10},
        )
        assert resp.status_code == 200
        result_ns = {r["namespace"] for r in resp.json()["results"]}
        assert "ns-r-alpha" in result_ns
        assert "ns-r-beta" in result_ns
    finally:
        await client.post("/memory/forget", json={"namespace": "ns-r-alpha", "key": "resolve-color"})
        await client.post("/memory/forget", json={"namespace": "ns-r-beta", "key": "resolve-food"})
        pool = await __import__("server.services.principal_service", fromlist=["get_pool"]).get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM principals WHERE name = $1", "ns-resolve-agent")


@pytest.mark.asyncio
async def test_default_store_is_permanent(client, db_pool):
    """Memories never expire by default. expires_at is only set when a
    positive expiration_days is passed explicitly (engram is a durable store)."""
    # Default store → permanent (expires_at IS NULL)
    resp = await client.post("/memory/set", json={
        "namespace": "test",
        "key": "ttl_default",
        "value": "should be permanent",
    })
    assert resp.status_code == 200
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT expires_at FROM memories WHERE namespace='test' AND key='ttl_default'"
        )
    assert row["expires_at"] is None

    # Explicit positive TTL → expires_at is set
    resp = await client.post("/memory/set", json={
        "namespace": "test",
        "key": "ttl_explicit",
        "value": "should expire",
        "expiration_days": 7,
    })
    assert resp.status_code == 200
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT expires_at FROM memories WHERE namespace='test' AND key='ttl_explicit'"
        )
    assert row["expires_at"] is not None

    # Clean up
    await client.post("/memory/forget", json={"namespace": "test", "key": "ttl_default"})
    await client.post("/memory/forget", json={"namespace": "test", "key": "ttl_explicit"})


@pytest.mark.asyncio
async def test_set_reports_created_vs_overwritten(client):
    """MEM-1: a write that REPLACES an existing value must say so.

    Memory identity is (namespace, key, scope, user_id, project) and carries
    no session dimension — deliberately, since the work outlives the session.
    The consequence is that two sessions in one project writing the same key
    destroy each other's value. Before this, both writes returned a
    byte-identical success and the loser had no way to know it had erased
    someone: a destructive outcome with no signal, the same shape as an
    unguarded bulk delete.
    """
    payload = {
        "namespace": "test", "key": "mem1/clobber", "value": "first writer",
        "scope": "machine", "user_id": "probe",
    }
    first = await client.post("/memory/set", json=payload)
    assert first.status_code == 200
    assert first.json()["created"] is True

    second = await client.post(
        "/memory/set", json={**payload, "value": "second writer"}
    )
    assert second.status_code == 200
    assert second.json()["created"] is False, "an overwrite must not look like a create"

    # And the overwrite really did land — the signal describes reality.
    got = await client.post("/memory/get", json={
        "namespace": "test", "key": "mem1/clobber",
        "scope": "machine", "user_id": "probe",
    })
    assert got.json()["memory"]["value"] == "second writer"

    await client.post("/memory/forget", json={
        "namespace": "test", "key": "mem1/clobber",
        "scope": "machine", "user_id": "probe",
    })


@pytest.mark.asyncio
async def test_distinct_keys_do_not_report_an_overwrite(client):
    """The signal must be specific — a different key is not a clobber."""
    base = {"namespace": "test", "scope": "machine", "user_id": "probe"}
    a = await client.post("/memory/set", json={**base, "key": "mem1/a", "value": "a"})
    b = await client.post("/memory/set", json={**base, "key": "mem1/b", "value": "b"})
    assert a.json()["created"] is True
    assert b.json()["created"] is True
    for k in ("mem1/a", "mem1/b"):
        await client.post("/memory/forget", json={**base, "key": k})
