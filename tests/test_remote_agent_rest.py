"""REMOTE-AGENT-1: what a bridge-less REST caller needs the server to do
for it, because no bridge fills these in."""

import pytest

from server.services import principal_service as ps


async def _cleanup(client, keys):
    for scope, user_id, key in keys:
        await client.post("/memory/forget", json={
            "namespace": "test", "key": key, "scope": scope, "user_id": user_id,
        })


@pytest.mark.asyncio
async def test_shared_scope_without_user_id_reads_and_writes_global(client):
    """(f) Every shared row lives under user_id 'global'. A REST caller that
    omits user_id used to land on 'default' and search an empty partition."""
    try:
        r = await client.post("/memory/set", json={
            "namespace": "test", "key": "ra1/shared-default",
            "value": "remote agents can find shared knowledge", "scope": "shared",
        })
        assert r.status_code == 200
        # the write went to global: an explicit-global get finds it
        got = await client.post("/memory/get", json={
            "namespace": "test", "key": "ra1/shared-default",
            "scope": "shared", "user_id": "global",
        })
        assert got.json()["memory"]["value"] == "remote agents can find shared knowledge"
        # and a bare search / keys call (no user_id) finds it too
        s = await client.post("/memory/search", json={
            "namespace": "test", "query": "remote agents shared knowledge",
            "scope": "shared",
        })
        assert any(h["key"] == "ra1/shared-default" for h in s.json()["results"])
        k = await client.post("/memory/keys", json={
            "namespace": "test", "scope": "shared", "prefix": "ra1/",
        })
        assert "ra1/shared-default" in [x["key"] if isinstance(x, dict) else x
                                         for x in k.json()["keys"]]
    finally:
        await _cleanup(client, [("shared", "global", "ra1/shared-default")])


@pytest.mark.asyncio
async def test_explicit_user_id_in_shared_scope_is_left_alone(client):
    try:
        await client.post("/memory/set", json={
            "namespace": "test", "key": "ra1/explicit", "value": "x",
            "scope": "shared", "user_id": "someone",
        })
        got = await client.post("/memory/get", json={
            "namespace": "test", "key": "ra1/explicit",
            "scope": "shared", "user_id": "global",
        })
        assert got.json().get("memory") is None
    finally:
        await _cleanup(client, [("shared", "someone", "ra1/explicit")])


@pytest.mark.asyncio
async def test_non_shared_scope_keeps_default_user_id(client):
    from server.models import MemorySearchRequest
    assert MemorySearchRequest(namespace="test", query="q", scope="user").user_id == "default"
    assert MemorySearchRequest(namespace="test", query="q", scope="shared").user_id == "global"


@pytest.mark.asyncio
async def test_search_hit_carries_the_version_get_would_return(client):
    """A hit's version must equal what /memory/get returns, so it can be
    passed straight to if_match — even when the hit is snippet-trimmed."""
    value = "\n".join(f"line {i} about versioned remote search" for i in range(20))
    try:
        await client.post("/memory/set", json={
            "namespace": "test", "key": "ra1/versioned", "value": value,
        })
        got = await client.post("/memory/get", json={
            "namespace": "test", "key": "ra1/versioned",
        })
        want = got.json()["memory"]["version"]
        assert want
        for extra in ({}, {"snippet_lines": 2}):
            s = await client.post("/memory/search", json={
                "namespace": "test", "query": "versioned remote search", **extra,
            })
            hit = [h for h in s.json()["results"] if h["key"] == "ra1/versioned"][0]
            assert hit["version"] == want, extra
    finally:
        await _cleanup(client, [("user", "default", "ra1/versioned")])


@pytest.mark.asyncio
async def test_whoami_names_the_callers_inbox_address(client):
    """(b) A seatless caller is addressed by its principal name; /whoami says
    so instead of leaving it to convention."""
    try:
        create = await client.post("/admin/principals", json={
            "name": "Remote-Agent-Probe", "type": "agent",
            "read_namespaces": ["test"], "write_namespaces": ["test"],
        })
        token = create.json()["raw_token"]
        data = (await client.get(
            "/whoami", headers={"Authorization": f"Bearer {token}"})).json()
        assert data["inbox_address"] == "remote-agent-probe"
        assert data["listen_set"][0] == "remote-agent-probe"
    finally:
        pool = await ps.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM principals WHERE lower(name) = 'remote-agent-probe'")
