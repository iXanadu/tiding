"""Namespace permission enforcement tests."""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import patch

from server.services import principal_service as ps


async def _cleanup_principal(name: str):
    pool = await ps.get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM principals WHERE name = $1", name)


@pytest_asyncio.fixture
async def enforced_client(services):
    """Client with require_auth=true. No legacy api_token."""
    with patch("server.auth.settings") as mock_settings, \
         patch("server.dependencies.settings") as mock_dep_settings:
        mock_settings.require_auth = True
        mock_settings.api_token = ""
        mock_dep_settings.require_auth = True
        mock_dep_settings.api_token = ""
        from server.main import app
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://localhost") as c:
            yield c


# --- Anonymous access when require_auth=false ---

@pytest.mark.asyncio
async def test_anonymous_allowed_when_not_enforced(client):
    """When require_auth=false and no api_token, anonymous access works."""
    resp = await client.post("/memory/get", json={
        "namespace": "test",
        "key": "anything",
    })
    assert resp.status_code == 200


# --- Enforcement mode: 401 without token ---

@pytest.mark.asyncio
async def test_no_token_returns_401_when_enforced(enforced_client):
    resp = await enforced_client.post("/memory/get", json={
        "namespace": "test",
        "key": "anything",
    })
    assert resp.status_code == 401


# --- Principal with write access can write ---

@pytest.mark.asyncio
async def test_principal_can_write_permitted_namespace(enforced_client):
    try:
        principal, raw_token = await ps.create_principal(
            name="perm-writer",
            type="agent",
            write_namespaces=["test"],
            read_namespaces=["test"],
        )
        resp = await enforced_client.post(
            "/memory/set",
            json={
                "namespace": "test",
                "key": "perm-test-key",
                "value": "hello",
            },
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert resp.status_code == 200

        # Clean up memory
        await enforced_client.post(
            "/memory/forget",
            json={"namespace": "test", "key": "perm-test-key"},
            headers={"Authorization": f"Bearer {raw_token}"},
        )
    finally:
        await _cleanup_principal("perm-writer")


# --- Principal without write access gets 403 ---

@pytest.mark.asyncio
async def test_principal_denied_write_to_other_namespace(enforced_client):
    try:
        _, raw_token = await ps.create_principal(
            name="perm-denied",
            type="agent",
            write_namespaces=["allowed-ns"],
            read_namespaces=["allowed-ns"],
        )
        resp = await enforced_client.post(
            "/memory/set",
            json={
                "namespace": "forbidden-ns",
                "key": "should-fail",
                "value": "nope",
            },
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert resp.status_code == 403
    finally:
        await _cleanup_principal("perm-denied")


# --- Read permission enforcement ---

@pytest.mark.asyncio
async def test_principal_denied_read_to_other_namespace(enforced_client):
    try:
        _, raw_token = await ps.create_principal(
            name="perm-read-denied",
            type="agent",
            write_namespaces=["my-ns"],
            read_namespaces=["my-ns"],
        )
        resp = await enforced_client.post(
            "/memory/search",
            json={"namespace": "other-ns", "query": "anything"},
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert resp.status_code == 403
    finally:
        await _cleanup_principal("perm-read-denied")


# --- Admin bypasses namespace checks ---

@pytest.mark.asyncio
async def test_admin_bypasses_namespace_check(enforced_client):
    try:
        _, raw_token = await ps.create_principal(
            name="perm-admin",
            type="agent",
            is_admin=True,
        )
        resp = await enforced_client.post(
            "/memory/get",
            json={"namespace": "any-namespace", "key": "anything"},
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert resp.status_code == 200
    finally:
        await _cleanup_principal("perm-admin")


# --- Wildcard namespace access ---

@pytest.mark.asyncio
async def test_wildcard_namespace_access(enforced_client):
    try:
        _, raw_token = await ps.create_principal(
            name="perm-wildcard",
            type="agent",
            read_namespaces=["*"],
            write_namespaces=["*"],
        )
        resp = await enforced_client.post(
            "/memory/get",
            json={"namespace": "any-ns", "key": "anything"},
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert resp.status_code == 200
    finally:
        await _cleanup_principal("perm-wildcard")


# --- Admin endpoints require admin when enforced ---

@pytest.mark.asyncio
async def test_admin_endpoint_requires_admin_when_enforced(enforced_client):
    try:
        _, raw_token = await ps.create_principal(
            name="perm-nonadmin",
            type="agent",
            read_namespaces=["*"],
        )
        resp = await enforced_client.get(
            "/admin/stats",
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert resp.status_code == 403
    finally:
        await _cleanup_principal("perm-nonadmin")


@pytest.mark.asyncio
async def test_admin_endpoint_allowed_for_admin_when_enforced(enforced_client):
    try:
        _, raw_token = await ps.create_principal(
            name="perm-admin-ep",
            type="agent",
            is_admin=True,
        )
        resp = await enforced_client.get(
            "/admin/stats",
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert resp.status_code == 200
    finally:
        await _cleanup_principal("perm-admin-ep")


# --- Anonymous-admin gate: non-loopback bind with require_auth=false ---
# (2026-07-21 audit: /admin/* + principal CRUD must not be anonymous when
# the server is reachable from the network, even in enrichment mode.)

def _unenforced_settings(mock_auth, mock_dep, *, host, api_token=""):
    mock_auth.require_auth = False
    mock_auth.api_token = api_token
    mock_auth.warn_unauthed = False
    mock_dep.require_auth = False
    mock_dep.api_token = api_token
    mock_dep.host = host


@pytest_asyncio.fixture
async def open_nonloopback_client(services):
    """require_auth=false, bound 0.0.0.0, NO legacy api_token (fully open)."""
    with patch("server.auth.settings") as ma, \
         patch("server.dependencies.settings") as md:
        _unenforced_settings(ma, md, host="0.0.0.0")
        from server.main import app
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://localhost") as c:
            yield c


@pytest_asyncio.fixture
async def legacy_nonloopback_client(services):
    """require_auth=false, bound 0.0.0.0, legacy api_token configured."""
    with patch("server.auth.settings") as ma, \
         patch("server.dependencies.settings") as md:
        _unenforced_settings(ma, md, host="0.0.0.0", api_token="legacy-secret")
        from server.main import app
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://localhost") as c:
            yield c


@pytest.mark.asyncio
async def test_admin_anonymous_401_on_nonloopback(open_nonloopback_client):
    resp = await open_nonloopback_client.get("/admin/stats")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_principals_anonymous_401_on_nonloopback(open_nonloopback_client):
    resp = await open_nonloopback_client.get("/admin/principals")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_memory_still_open_on_nonloopback(open_nonloopback_client):
    """The gate covers the admin surface only — memory endpoints unchanged."""
    resp = await open_nonloopback_client.post(
        "/memory/get", json={"namespace": "test", "key": "anything"}
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_admin_legacy_token_ok_on_nonloopback(legacy_nonloopback_client):
    resp = await legacy_nonloopback_client.get(
        "/admin/stats", headers={"Authorization": "Bearer legacy-secret"}
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_admin_nonadmin_principal_403_on_nonloopback(open_nonloopback_client):
    try:
        _, raw_token = await ps.create_principal(
            name="gate-nonadmin",
            type="agent",
            read_namespaces=["*"],
        )
        resp = await open_nonloopback_client.get(
            "/admin/stats", headers={"Authorization": f"Bearer {raw_token}"}
        )
        assert resp.status_code == 403
    finally:
        await _cleanup_principal("gate-nonadmin")


@pytest.mark.asyncio
async def test_admin_admin_principal_ok_on_nonloopback(open_nonloopback_client):
    try:
        _, raw_token = await ps.create_principal(
            name="gate-admin",
            type="agent",
            is_admin=True,
        )
        resp = await open_nonloopback_client.get(
            "/admin/stats", headers={"Authorization": f"Bearer {raw_token}"}
        )
        assert resp.status_code == 200
    finally:
        await _cleanup_principal("gate-admin")


@pytest.mark.asyncio
async def test_admin_anonymous_still_open_on_loopback(client):
    """Loopback bind keeps the documented local-open posture."""
    resp = await client.get("/admin/stats")
    assert resp.status_code == 200


# --- PATCH /admin/memories: namespace write-checks for non-admin principals ---

@pytest.mark.asyncio
async def test_patch_memories_new_namespace_requires_write(client):
    """A non-admin principal (enrichment mode) can't move memories into a
    namespace it lacks write access to."""
    try:
        _, raw_token = await ps.create_principal(
            name="patch-mover",
            type="agent",
            read_namespaces=["test"],
            write_namespaces=["test"],
        )
        resp = await client.patch(
            "/admin/memories",
            json={
                "namespace": "test",
                "key": "any-key",
                "scope": "machine",
                "user_id": "any",
                "new_namespace": "forbidden-ns",
            },
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert resp.status_code == 403
    finally:
        await _cleanup_principal("patch-mover")


@pytest.mark.asyncio
async def test_patch_memories_source_namespace_requires_write(client):
    try:
        _, raw_token = await ps.create_principal(
            name="patch-nosrc",
            type="agent",
            write_namespaces=["elsewhere"],
        )
        resp = await client.patch(
            "/admin/memories",
            json={
                "namespace": "test",
                "key": "any-key",
                "scope": "machine",
                "user_id": "any",
            },
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert resp.status_code == 403
    finally:
        await _cleanup_principal("patch-nosrc")
