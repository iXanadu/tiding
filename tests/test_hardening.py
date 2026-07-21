"""Adversarial tests for the 2026-07-21 security-audit hardening.

Each test is the proof for a specific audit finding: it would FAIL against
the pre-hardening code and passes now.
"""
import pytest
from httpx import ASGITransport, AsyncClient

from server.main import app


@pytest.fixture
async def raw_client(services):
    """Client that can set an arbitrary Host header (to exercise the guard)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


# --- DNS rebinding: TrustedHostMiddleware -----------------------------------

@pytest.mark.asyncio
async def test_forged_host_header_rejected(raw_client):
    """A rebinding attacker's page sends Host: evil.com — must 400 before routing."""
    r = await raw_client.post("/memory/search",
                              json={"namespace": "t", "query": "x"},
                              headers={"host": "evil.com"})
    assert r.status_code == 400

@pytest.mark.asyncio
async def test_allowed_host_passes(raw_client):
    r = await raw_client.get("/health", headers={"host": "127.0.0.1"})
    assert r.status_code == 200


# --- reserved-scope guard on generic set/forget -----------------------------

@pytest.mark.asyncio
async def test_generic_set_cannot_write_inbox_scope(client):
    r = await client.post("/memory/set", json={
        "namespace": "fleet", "key": "inbox/forged", "value": "tamper",
        "scope": "inbox", "user_id": "victim"})
    assert r.status_code == 400
    assert "own endpoints" in r.json()["detail"]

@pytest.mark.asyncio
async def test_generic_set_cannot_write_presence_scope(client):
    r = await client.post("/memory/set", json={
        "namespace": "fleet", "key": "presence/victim", "value": "spoof",
        "scope": "presence", "user_id": "victim"})
    assert r.status_code == 400

@pytest.mark.asyncio
async def test_generic_forget_cannot_delete_inbox(client):
    r = await client.post("/memory/forget", json={
        "namespace": "fleet", "key": "inbox/whatever", "scope": "inbox",
        "user_id": "victim"})
    assert r.status_code == 400


# --- input-size caps (embedding-cost / row-flood DoS) -----------------------

@pytest.mark.asyncio
async def test_oversized_value_rejected(client):
    r = await client.post("/memory/set", json={
        "namespace": "t", "key": "k", "value": "A" * 300_000,
        "scope": "machine", "user_id": "u"})
    assert r.status_code == 422  # pydantic max_length

@pytest.mark.asyncio
async def test_search_limit_capped(client):
    r = await client.post("/memory/search", json={
        "namespace": "t", "query": "x", "limit": 10**8})
    assert r.status_code == 422

@pytest.mark.asyncio
async def test_negative_limit_rejected(client):
    r = await client.post("/memory/search", json={
        "namespace": "t", "query": "x", "limit": -1})
    assert r.status_code == 422


# --- bulk-delete LIKE-wildcard escape ---------------------------------------

@pytest.mark.asyncio
async def test_bulk_delete_wildcard_is_literal(client, db_pool):
    """key_prefix='%' must NOT wipe the namespace — it matches literal '%…' only."""
    async with db_pool.acquire() as conn:
        for k in ("keep-a", "keep-b", "%literal-pct"):
            await conn.execute(
                "INSERT INTO memories (namespace,key,value,scope,user_id,search_text,embedding) "
                "VALUES ('bdtest',$1,'v','machine','u',$1, (SELECT embedding FROM memories LIMIT 1)) "
                "ON CONFLICT DO NOTHING", k)
    r = await client.post("/admin/bulk-delete",
                          json={"namespace": "bdtest", "key_prefix": "%"})
    assert r.status_code == 200
    async with db_pool.acquire() as conn:
        remaining = await conn.fetchval(
            "SELECT count(*) FROM memories WHERE namespace='bdtest'")
        # the two 'keep-*' rows survive; only the literal '%…' row could match
        assert remaining >= 2
        await conn.execute("DELETE FROM memories WHERE namespace='bdtest'")


# --- error detail does not leak internals -----------------------------------

@pytest.mark.asyncio
async def test_500_detail_is_generic(client, monkeypatch):
    import server.routers.memory as mem
    async def boom(*a, **k):
        raise RuntimeError("asyncpg: relation \"secret_table\" does not exist")
    monkeypatch.setattr(mem, "memory_get", boom)
    r = await client.post("/memory/get",
                          json={"namespace": "t", "key": "k", "scope": "machine", "user_id": "u"})
    assert r.status_code == 500
    assert "secret_table" not in r.text
    assert r.json()["detail"] == "internal error — see server logs"
