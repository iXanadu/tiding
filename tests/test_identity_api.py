"""HTTP-level tests for /whoami and /namespaces (caller-scoped identity endpoints)."""

import pytest

from server.services import principal_service as ps


async def _cleanup_principal(name: str):
    pool = await ps.get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM principals WHERE name = $1", name)


# --- /whoami ---

@pytest.mark.asyncio
async def test_whoami_requires_principal(client):
    """No Authorization header → 401 (require_principal raises)."""
    resp = await client.get("/whoami")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_whoami_invalid_token(client):
    resp = await client.get("/whoami", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_whoami_returns_principal(client):
    try:
        create = await client.post(
            "/admin/principals",
            json={
                "name": "whoami-agent",
                "type": "agent",
                "read_namespaces": ["claude-code", "beast"],
                "write_namespaces": ["beast"],
            },
        )
        token = create.json()["raw_token"]

        resp = await client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "whoami-agent"
        assert data["type"] == "agent"
        assert data["is_admin"] is False
        assert data["active"] is True
        assert data["read_namespaces"] == ["claude-code", "beast"]
        assert data["write_namespaces"] == ["beast"]
    finally:
        await _cleanup_principal("whoami-agent")


@pytest.mark.asyncio
async def test_whoami_for_admin(client):
    try:
        create = await client.post(
            "/admin/principals",
            json={
                "name": "whoami-admin",
                "type": "agent",
                "is_admin": True,
                "read_namespaces": ["*"],
                "write_namespaces": ["*"],
            },
        )
        token = create.json()["raw_token"]
        resp = await client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["is_admin"] is True
    finally:
        await _cleanup_principal("whoami-admin")


# --- /namespaces ---

@pytest.mark.asyncio
async def test_namespaces_requires_principal(client):
    resp = await client.get("/namespaces")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_namespaces_explicit_lists(client):
    """Non-wildcard principal: return the configured read/write lists verbatim."""
    try:
        create = await client.post(
            "/admin/principals",
            json={
                "name": "ns-agent-explicit",
                "type": "agent",
                "read_namespaces": ["claude-code", "ha"],
                "write_namespaces": ["claude-code"],
            },
        )
        token = create.json()["raw_token"]
        resp = await client.get("/namespaces", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert sorted(data["read"]) == ["claude-code", "ha"]
        assert data["write"] == ["claude-code"]
    finally:
        await _cleanup_principal("ns-agent-explicit")


@pytest.mark.asyncio
async def test_namespaces_wildcard_expands(client):
    """Wildcard read: expand to concrete namespaces from the DB."""
    try:
        create = await client.post(
            "/admin/principals",
            json={
                "name": "ns-agent-wild",
                "type": "agent",
                "read_namespaces": ["*"],
                "write_namespaces": ["beast"],
            },
        )
        token = create.json()["raw_token"]
        resp = await client.get("/namespaces", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert "*" not in data["read"]
        assert data["write"] == ["beast"]
    finally:
        await _cleanup_principal("ns-agent-wild")
