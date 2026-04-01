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
    """Omitting namespace should return 422."""
    resp = await client.post("/memory/set", json={
        "key": "no_ns",
        "value": "should fail",
    })
    assert resp.status_code == 422

    resp = await client.post("/memory/get", json={"key": "no_ns"})
    assert resp.status_code == 422

    resp = await client.post("/memory/search", json={"query": "anything"})
    assert resp.status_code == 422

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
async def test_search_namespace_or_namespaces_required(client):
    """Must provide either namespace or namespaces, not neither."""
    resp = await client.post("/memory/search", json={
        "query": "anything",
    })
    assert resp.status_code == 422
