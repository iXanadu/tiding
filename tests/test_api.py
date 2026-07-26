"""Integration tests for the HTTP API endpoints."""

import asyncio

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


@pytest.mark.asyncio
async def test_if_match_allows_the_write_when_unchanged(client):
    """MEM-4: the normal read-modify-write path succeeds."""
    base = {"namespace": "test", "key": "mem4/doc", "scope": "machine",
            "user_id": "probe"}
    first = await client.post("/memory/set", json={**base, "value": "## a\nv1"})
    v = first.json()["version"]
    assert v

    ok = await client.post(
        "/memory/set", json={**base, "value": "## a\nv2", "if_match": v}
    )
    assert ok.status_code == 200
    assert ok.json()["version"] != v, "version must move when the value changes"
    await client.post("/memory/forget", json=base)


@pytest.mark.asyncio
async def test_if_match_refuses_a_lost_update(client):
    """The motivating case: two agents rewrite their own section of one doc.

    Both read the same version. The first write wins. The second must be
    REFUSED — under a blind write it would silently discard the first agent's
    section, which is the exact loss this guard exists to prevent.
    """
    base = {"namespace": "test", "key": "mem4/shared", "scope": "machine",
            "user_id": "probe"}
    start = await client.post("/memory/set", json={**base, "value": "## a\n## b"})
    stale = start.json()["version"]

    winner = await client.post(
        "/memory/set", json={**base, "value": "## a (edited)\n## b", "if_match": stale}
    )
    assert winner.status_code == 200

    loser = await client.post(
        "/memory/set", json={**base, "value": "## a\n## b (edited)", "if_match": stale}
    )
    assert loser.status_code == 409
    detail = loser.json()["detail"]
    assert detail["error"] == "version_conflict"
    # The 409 must carry the current value so the loser can re-merge without
    # another round trip.
    assert detail["current_value"] == "## a (edited)\n## b"
    assert detail["current_version"] == winner.json()["version"]

    # The winner's edit survived — the refused write changed nothing.
    got = await client.post("/memory/get", json=base)
    assert got.json()["memory"]["value"] == "## a (edited)\n## b"
    await client.post("/memory/forget", json=base)


@pytest.mark.asyncio
async def test_concurrent_conditional_writes_only_one_wins(client):
    """Under genuine concurrency exactly one conditional write may succeed.

    The check and the write share a transaction with the row locked; without
    that, both writers see a matching version and both proceed — the guard
    would have the very race it exists to close.
    """
    base = {"namespace": "test", "key": "mem4/race", "scope": "machine",
            "user_id": "probe"}
    start = await client.post("/memory/set", json={**base, "value": "seed"})
    v = start.json()["version"]

    results = await asyncio.gather(*(
        client.post("/memory/set", json={**base, "value": f"writer-{i}", "if_match": v})
        for i in range(8)
    ))
    codes = [r.status_code for r in results]
    assert codes.count(200) == 1, f"exactly one writer may win, got {codes}"
    assert codes.count(409) == 7
    await client.post("/memory/forget", json=base)


@pytest.mark.asyncio
async def test_if_match_empty_string_asserts_absence(client):
    """`if_match=""` means "I believe this key is unused" — create-only."""
    base = {"namespace": "test", "key": "mem4/create-only", "scope": "machine",
            "user_id": "probe"}
    made = await client.post("/memory/set", json={**base, "value": "mine", "if_match": ""})
    assert made.status_code == 200

    again = await client.post("/memory/set", json={**base, "value": "yours", "if_match": ""})
    assert again.status_code == 409, "the row exists now; absence must not be asserted twice"
    await client.post("/memory/forget", json=base)


@pytest.mark.asyncio
async def test_concurrent_create_only_writes_only_one_wins(client):
    """The must-not-exist guard must hold under concurrency, not just in sequence.

    Reported by AgentBeast 2026-07-26 after it destroyed real content on a
    first live run: two writers raced on a fresh key, BOTH got 200, and both
    responses carried ``if_match_applied: true``.

    The absence check could not be a read-then-write, because SELECT ... FOR
    UPDATE has nothing to lock when the row does not exist yet — so both
    writers read "absent", both passed the guard, and the second one's upsert
    took the ON CONFLICT DO UPDATE branch straight over the first's content.
    The row-lock that closes the update race is structurally incapable of
    closing this one.

    This is the worse half of the bug: a silent overwrite still reports the
    positive signal callers were told to trust.
    """
    base = {"namespace": "test", "key": "mem4/create-race", "scope": "machine",
            "user_id": "probe"}
    await client.post("/memory/forget", json=base)

    results = await asyncio.gather(*(
        client.post("/memory/set", json={**base, "value": f"writer-{i}", "if_match": ""})
        for i in range(8)
    ))
    codes = [r.status_code for r in results]
    assert codes.count(200) == 1, f"exactly one create may win, got {codes}"
    assert codes.count(409) == 7

    # and the winner's content must be what is actually stored
    winner = [r for r in results if r.status_code == 200][0]
    assert winner.json()["created"] is True
    got = await client.post("/memory/get", json=base)
    assert got.json()["memory"]["value"].startswith("writer-")
    await client.post("/memory/forget", json=base)


@pytest.mark.asyncio
async def test_a_losing_create_never_claims_it_was_guarded(client):
    """`if_match_applied` must never report true on a write that was applied
    unguarded — that is the signal callers gate their merges on."""
    base = {"namespace": "test", "key": "mem4/create-signal", "scope": "machine",
            "user_id": "probe"}
    await client.post("/memory/forget", json=base)

    results = await asyncio.gather(*(
        client.post("/memory/set", json={**base, "value": f"w{i}", "if_match": ""})
        for i in range(6)
    ))
    for r in results:
        if r.status_code == 200:
            # a successful conditional create is a genuine create, never a
            # silent overwrite wearing a create's response
            assert r.json()["created"] is True, (
                "created=false on if_match='' means the key existed and the "
                "guard failed open"
            )
    await client.post("/memory/forget", json=base)


@pytest.mark.asyncio
async def test_omitting_if_match_is_unconditional(client):
    """Back-compat: every existing caller keeps today's behavior exactly."""
    base = {"namespace": "test", "key": "mem4/compat", "scope": "machine",
            "user_id": "probe"}
    await client.post("/memory/set", json={**base, "value": "one"})
    second = await client.post("/memory/set", json={**base, "value": "two"})
    assert second.status_code == 200
    assert second.json()["created"] is False
    await client.post("/memory/forget", json=base)


@pytest.mark.asyncio
async def test_conditional_write_is_positively_confirmed(client):
    """MEM-4 safety signal: the server states whether the guard actually ran.

    A client sending `if_match` to a server that PREDATES this feature has the
    field silently dropped by pydantic and its write proceeds UNGUARDED while
    it believes it was protected — the `confirm: false` shape that cost 1733
    inbox rows. An old server cannot be fixed retroactively, so the signal
    must be something only a NEW server emits. Absence must never read as
    success.
    """
    base = {"namespace": "test", "key": "mem4/signal", "scope": "machine",
            "user_id": "probe"}
    made = await client.post("/memory/set", json={**base, "value": "v1"})
    assert made.json()["if_match_applied"] is False, "unconditional write must say so"

    guarded = await client.post("/memory/set", json={
        **base, "value": "v2", "if_match": made.json()["version"]
    })
    assert guarded.json()["if_match_applied"] is True, (
        "a client must be able to confirm the guard ran, not infer it"
    )
    await client.post("/memory/forget", json=base)


@pytest.mark.asyncio
async def test_a_misspelled_if_match_fails_closed(client):
    """A typo'd guard field must report UNGUARDED, not silently pass.

    `if_matched` (note the 'ed') is an unknown field, so pydantic drops it and
    the write is unconditional. The signal must say so — because the caller's
    one-line check (`if_match_applied is True`) is then False and it declines
    to merge. Loud-but-broken beats silent-and-wrong.

    This is why the signal is derived from what the server ACTUALLY DID rather
    than from what the request appeared to ask for: it reports the truth for
    every reason the guard might not have run — old server, typo, or omission
    — without needing to enumerate them. Traced by agentbeast 2026-07-24;
    pinned here so it stays true.
    """
    base = {"namespace": "test", "key": "mem4/typo", "scope": "machine",
            "user_id": "probe"}
    first = await client.post("/memory/set", json={**base, "value": "v1"})
    assert first.status_code == 200

    typo = await client.post("/memory/set", json={
        **base, "value": "v2", "if_matched": first.json()["version"],
    })
    assert typo.status_code == 200, "unknown fields are ignored, not rejected"
    assert typo.json()["if_match_applied"] is False, (
        "a typo'd guard field must report that no guard ran"
    )
    await client.post("/memory/forget", json=base)
