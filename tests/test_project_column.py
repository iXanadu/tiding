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


@pytest.mark.asyncio
async def test_project_search_spans_all_writers_with_wildcard(client):
    """user_id='*' returns every writer's rows in the project (MEM-5).

    Project memory belongs to the PROJECT. Partitioning reads by the writing
    principal made a note invisible to every peer but its author, which is how
    two agents on one project each read a fraction of it and neither could tell
    that from an empty project.
    """
    writers = ("writer-alpha", "writer-beta")
    try:
        for w in writers:
            resp = await client.post("/memory/set", json={
                "namespace": "fleet",
                "key": f"span-test-{w}",
                "value": f"orbital mechanics briefing authored by {w}",
                "scope": "project",
                "user_id": w,
                "project": "spantest",
            })
            assert resp.status_code == 200

        # Each writer alone sees only its own row.
        resp = await client.post("/memory/search", json={
            "namespaces": ["fleet"], "query": "orbital mechanics briefing",
            "scope": "project", "user_id": "writer-alpha",
            "project": "spantest", "limit": 10,
        })
        assert resp.status_code == 200
        own = resp.json()["results"]
        assert {r["user_id"] for r in own} == {"writer-alpha"}

        # The wildcard spans both, and provenance survives in the results.
        resp = await client.post("/memory/search", json={
            "namespaces": ["fleet"], "query": "orbital mechanics briefing",
            "scope": "project", "user_id": "*",
            "project": "spantest", "limit": 10,
        })
        assert resp.status_code == 200
        spanned = resp.json()["results"]
        assert {r["user_id"] for r in spanned} == set(writers)
    finally:
        for w in writers:
            await client.post("/memory/forget", json={
                "namespace": "fleet", "key": f"span-test-{w}",
                "scope": "project", "user_id": w, "project": "spantest",
            })


@pytest.mark.asyncio
async def test_wildcard_is_literal_outside_project_scope(client):
    """scope=user must NOT span writers — there user_id is a PERSON.

    The wildcard fixes a partition that scopes nothing (project rows are
    already scoped by the project column). It must never become a way to read
    across people or hosts, so outside scope=project it stays a literal.
    """
    try:
        resp = await client.post("/memory/set", json={
            "namespace": "fleet",
            "key": "private-user-row",
            "value": "sextant calibration notes belonging to one person",
            "scope": "user",
            "user_id": "person-alpha",
        })
        assert resp.status_code == 200

        resp = await client.post("/memory/search", json={
            "namespaces": ["fleet"], "query": "sextant calibration notes",
            "scope": "user", "user_id": "*", "limit": 10,
        })
        assert resp.status_code == 200
        assert resp.json()["results"] == []
    finally:
        await client.post("/memory/forget", json={
            "namespace": "fleet", "key": "private-user-row",
            "scope": "user", "user_id": "person-alpha",
        })


@pytest.mark.asyncio
async def test_truncation_hint_names_the_writer_for_project_rows(client):
    """The 'full text' hint must be a call that WORKS for another's row.

    Project search spans writers, but memory_get resolves to the caller's own
    partition — so a hint without user_id points at nothing for any row the
    caller did not write, advertising a retrieval that then fails.
    """
    long_value = "\n".join(f"line {i}" for i in range(40))
    try:
        resp = await client.post("/memory/set", json={
            "namespace": "fleet", "key": "hint-test-key", "value": long_value,
            "scope": "project", "user_id": "writer-gamma", "project": "hinttest",
        })
        assert resp.status_code == 200

        resp = await client.post("/memory/search", json={
            "namespaces": ["fleet"], "query": "line", "scope": "project",
            "user_id": "*", "project": "hinttest", "limit": 5, "snippet_lines": 3,
        })
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert results, "expected the long row back"
        assert "TRUNCATED" in results[0]["value"]
        assert "user_id='writer-gamma'" in results[0]["value"]
    finally:
        await client.post("/memory/forget", json={
            "namespace": "fleet", "key": "hint-test-key",
            "scope": "project", "user_id": "writer-gamma", "project": "hinttest",
        })
