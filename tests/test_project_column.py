"""Phase 4: project column tests.

The (user_id, project) pair partitions scope=project rows so multiple
people can have memories under the same key for different projects, and
one person can have memories under the same key in different projects."""

import pytest


@pytest.mark.asyncio
async def test_project_scope_set_get_roundtrip(client):
    """Round-trip: set with user_id=person + project, read back with same."""
    try:
        # ixanadu's wip/current in project engram
        resp = await client.post("/memory/set", json={
            "namespace": "fleet",
            "key": "phase4-test-key",
            "value": "ixanadu writes to engram",
            "scope": "project",
            "user_id": "ixanadu",
            "project": "engram",
        })
        assert resp.status_code == 200

        # Get it back
        resp = await client.post("/memory/get", json={
            "namespace": "fleet",
            "key": "phase4-test-key",
            "scope": "project",
            "user_id": "ixanadu",
            "project": "engram",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["memory"]["value"] == "ixanadu writes to engram"
        assert data["memory"]["project"] == "engram"
        assert data["memory"]["user_id"] == "ixanadu"
    finally:
        await client.post("/memory/forget", json={
            "namespace": "fleet",
            "key": "phase4-test-key",
            "scope": "project",
            "user_id": "ixanadu",
            "project": "engram",
        })


@pytest.mark.asyncio
async def test_project_isolation_same_key_different_project(client):
    """Same (namespace, key, scope, user_id) but different project →
    two distinct rows, no collision."""
    try:
        await client.post("/memory/set", json={
            "namespace": "fleet",
            "key": "phase4-iso",
            "value": "engram value",
            "scope": "project",
            "user_id": "ixanadu",
            "project": "engram",
        })
        await client.post("/memory/set", json={
            "namespace": "fleet",
            "key": "phase4-iso",
            "value": "projalpha value",
            "scope": "project",
            "user_id": "ixanadu",
            "project": "projalpha",
        })

        # Both exist independently
        r1 = await client.post("/memory/get", json={
            "namespace": "fleet",
            "key": "phase4-iso",
            "scope": "project",
            "user_id": "ixanadu",
            "project": "engram",
        })
        r2 = await client.post("/memory/get", json={
            "namespace": "fleet",
            "key": "phase4-iso",
            "scope": "project",
            "user_id": "ixanadu",
            "project": "projalpha",
        })
        assert r1.json()["memory"]["value"] == "engram value"
        assert r2.json()["memory"]["value"] == "projalpha value"
    finally:
        for proj in ("engram", "projalpha"):
            await client.post("/memory/forget", json={
                "namespace": "fleet",
                "key": "phase4-iso",
                "scope": "project",
                "user_id": "ixanadu",
                "project": proj,
            })


@pytest.mark.asyncio
async def test_non_project_scopes_unchanged(client):
    """scope=user/shared/machine ignore project (it stays NULL)."""
    try:
        resp = await client.post("/memory/set", json={
            "namespace": "fleet",
            "key": "phase4-user-scope",
            "value": "user-scope memory",
            "scope": "user",
            "user_id": "alice",
        })
        assert resp.status_code == 200

        resp = await client.post("/memory/get", json={
            "namespace": "fleet",
            "key": "phase4-user-scope",
            "scope": "user",
            "user_id": "alice",
        })
        assert resp.status_code == 200
        assert resp.json()["memory"]["project"] is None
    finally:
        await client.post("/memory/forget", json={
            "namespace": "fleet",
            "key": "phase4-user-scope",
            "scope": "user",
            "user_id": "alice",
        })
