"""HTTP-level tests for principal CRUD endpoints."""

import pytest

from server.services import principal_service as ps


async def _cleanup_principal(name: str):
    pool = await ps.get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM principals WHERE name = $1", name)


# --- Create ---

@pytest.mark.asyncio
async def test_create_agent(client):
    try:
        resp = await client.post("/admin/principals", json={
            "name": "api-test-agent",
            "type": "agent",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["principal"]["name"] == "api-test-agent"
        assert data["principal"]["type"] == "agent"
        assert data["principal"]["has_token"] is True
        assert data["raw_token"] is not None
        assert data["raw_token"].startswith("engram_")
    finally:
        await _cleanup_principal("api-test-agent")


@pytest.mark.asyncio
async def test_create_human(client):
    try:
        resp = await client.post("/admin/principals", json={
            "name": "api-test-human",
            "type": "human",
            "password": "secret",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["principal"]["type"] == "human"
        assert data["principal"]["has_password"] is True
        assert data["principal"]["has_token"] is False
        assert data["raw_token"] is None
    finally:
        await _cleanup_principal("api-test-human")


@pytest.mark.asyncio
async def test_create_duplicate_returns_409(client):
    try:
        await client.post("/admin/principals", json={
            "name": "api-test-dup",
            "type": "agent",
        })
        resp = await client.post("/admin/principals", json={
            "name": "api-test-dup",
            "type": "agent",
        })
        assert resp.status_code == 409
    finally:
        await _cleanup_principal("api-test-dup")


# --- List ---

@pytest.mark.asyncio
async def test_list_principals(client):
    try:
        await client.post("/admin/principals", json={"name": "api-list-a", "type": "agent"})
        await client.post("/admin/principals", json={"name": "api-list-h", "type": "human"})

        resp = await client.get("/admin/principals")
        assert resp.status_code == 200
        names = [p["name"] for p in resp.json()["principals"]]
        assert "api-list-a" in names
        assert "api-list-h" in names

        resp = await client.get("/admin/principals?type=agent")
        names = [p["name"] for p in resp.json()["principals"]]
        assert "api-list-a" in names
        assert "api-list-h" not in names
    finally:
        await _cleanup_principal("api-list-a")
        await _cleanup_principal("api-list-h")


# --- Get ---

@pytest.mark.asyncio
async def test_get_principal(client):
    try:
        await client.post("/admin/principals", json={"name": "api-get-test", "type": "agent"})
        resp = await client.get("/admin/principals/api-get-test")
        assert resp.status_code == 200
        assert resp.json()["name"] == "api-get-test"
    finally:
        await _cleanup_principal("api-get-test")


@pytest.mark.asyncio
async def test_get_nonexistent_returns_404(client):
    resp = await client.get("/admin/principals/no-such-principal-xyz")
    assert resp.status_code == 404


# --- Update ---

@pytest.mark.asyncio
async def test_update_principal(client):
    try:
        await client.post("/admin/principals", json={"name": "api-upd-test", "type": "agent"})
        resp = await client.patch("/admin/principals/api-upd-test", json={
            "is_admin": True,
            "read_namespaces": ["fleet", "beast"],
            "write_namespaces": ["fleet"],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_admin"] is True
        assert data["read_namespaces"] == ["fleet", "beast"]
        assert data["write_namespaces"] == ["fleet"]
    finally:
        await _cleanup_principal("api-upd-test")


@pytest.mark.asyncio
async def test_update_nonexistent_returns_404(client):
    resp = await client.patch("/admin/principals/no-such-xyz", json={"is_admin": True})
    assert resp.status_code == 404


# --- Deactivate ---

@pytest.mark.asyncio
async def test_deactivate_principal(client):
    try:
        await client.post("/admin/principals", json={"name": "api-deact-test", "type": "agent"})
        resp = await client.delete("/admin/principals/api-deact-test")
        assert resp.status_code == 200
        assert resp.json()["active"] is False

        # Verify it's deactivated
        resp = await client.get("/admin/principals/api-deact-test")
        assert resp.json()["active"] is False
    finally:
        await _cleanup_principal("api-deact-test")


@pytest.mark.asyncio
async def test_deactivate_nonexistent_returns_404(client):
    resp = await client.delete("/admin/principals/no-such-xyz")
    assert resp.status_code == 404


# --- Token regeneration ---

@pytest.mark.asyncio
async def test_regenerate_token(client):
    try:
        create_resp = await client.post("/admin/principals", json={
            "name": "api-regen-test",
            "type": "agent",
        })
        old_token = create_resp.json()["raw_token"]

        resp = await client.post("/admin/principals/api-regen-test/token")
        assert resp.status_code == 200
        new_token = resp.json()["raw_token"]
        assert new_token != old_token
        assert new_token.startswith("engram_")
    finally:
        await _cleanup_principal("api-regen-test")


# --- Aliases ---

@pytest.mark.asyncio
async def test_alias_crud(client):
    try:
        await client.post("/admin/principals", json={"name": "api-alias-test", "type": "human"})

        # Add alias
        resp = await client.post("/admin/principals/api-alias-test/aliases", json={
            "alias": "test-uuid-123",
            "source": "ha",
        })
        assert resp.status_code == 200
        assert resp.json()["alias"] == "test-uuid-123"

        # List aliases
        resp = await client.get("/admin/principals/api-alias-test/aliases")
        assert resp.status_code == 200
        assert len(resp.json()["aliases"]) == 1

        # Remove alias
        resp = await client.request(
            "DELETE",
            "/admin/principals/api-alias-test/aliases",
            json={"alias": "test-uuid-123", "source": "ha"},
        )
        assert resp.status_code == 200

        # Verify removed
        resp = await client.get("/admin/principals/api-alias-test/aliases")
        assert len(resp.json()["aliases"]) == 0
    finally:
        await _cleanup_principal("api-alias-test")


@pytest.mark.asyncio
async def test_alias_nonexistent_principal_returns_404(client):
    resp = await client.post("/admin/principals/no-such-xyz/aliases", json={
        "alias": "test",
        "source": "ha",
    })
    assert resp.status_code == 404
