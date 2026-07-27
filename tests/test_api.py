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


# --- Per-sender unread summary (session-card badge) ----------------------

def _mk_inbox(client, to, body, from_, thread_id=None, participants=None):
    payload = {"to": to, "body": body, "from": from_}
    if thread_id:
        payload["thread_id"] = thread_id
    return client.post("/memory/send", json=payload)


@pytest.mark.asyncio
async def test_unread_summary_counts_direct_mail_per_sender(client, db_pool):
    """The badge question: which agent has something for me that I haven't read."""
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE scope='inbox' AND user_id='badgeuser'")

    await client.post("/memory/send", json={
        "to": "badgeuser", "body": "one", "from_": "alpha-claude"})
    await client.post("/memory/send", json={
        "to": "badgeuser", "body": "two", "from_": "alpha-claude"})
    await client.post("/memory/send", json={
        "to": "badgeuser", "body": "three", "from_": "beta-claude"})

    r = await client.post("/memory/inbox/unread-summary", json={
        "listen_set": ["badgeuser"], "reader_identity": "badgeuser@host"})
    assert r.status_code == 200
    body = r.json()
    counts = {s["from"]: s["unread"] for s in body["senders"]}
    assert counts["alpha-claude"] == 2
    assert counts["beta-claude"] == 1
    assert body["total"] == 3

    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE scope='inbox' AND user_id='badgeuser'")


@pytest.mark.asyncio
async def test_unread_summary_excludes_group_traffic(client, db_pool):
    """DIRECT ONLY. A group message is not waiting on any one reader, so
    counting it against a single card would misreport a shared conversation
    as a personal obligation. Both group shapes must be excluded: engram's
    native fan-out (participants set) and a relay's `huddle/...` thread."""
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE scope='inbox' AND user_id='badgeuser2'")

    # direct — counts
    await client.post("/memory/send", json={
        "to": "badgeuser2", "body": "direct", "from_": "alpha-claude"})
    # native fan-out (>1 recipient mints a participants set) — must NOT count
    await client.post("/memory/send", json={
        "to": "badgeuser2, someone-else", "body": "fanout", "from_": "alpha-claude"})
    # relay huddle thread — must NOT count
    await client.post("/memory/send", json={
        "to": "badgeuser2", "body": "huddle relay", "from_": "alpha-claude",
        "thread_id": "huddle/ABC123"})

    r = await client.post("/memory/inbox/unread-summary", json={
        "listen_set": ["badgeuser2"], "reader_identity": "badgeuser2@host"})
    body = r.json()
    assert body["total"] == 1, f"group traffic leaked into the badge: {body}"
    assert body["senders"][0]["unread"] == 1

    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='inbox' AND user_id IN ('badgeuser2','someone-else')")


@pytest.mark.asyncio
async def test_unread_summary_drops_to_zero_once_acked(client, db_pool):
    """The badge must clear when the human actually reads. This is the whole
    contract: a surface that renders without acking shows a climbing count
    against a correspondent the user is current with."""
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE scope='inbox' AND user_id='badgeuser3'")

    sent = await client.post("/memory/send", json={
        "to": "badgeuser3", "body": "read me", "from_": "alpha-claude"})
    j = sent.json()
    msg_id = (j.get("ids") or [j["id"]])[0]

    before = await client.post("/memory/inbox/unread-summary", json={
        "listen_set": ["badgeuser3"], "reader_identity": "badgeuser3@host"})
    assert before.json()["total"] == 1

    await client.post(f"/memory/inbox/{msg_id}/ack",
                      json={"reader_identity": "badgeuser3@host"})

    after = await client.post("/memory/inbox/unread-summary", json={
        "listen_set": ["badgeuser3"], "reader_identity": "badgeuser3@host"})
    assert after.json()["total"] == 0, "badge did not clear after the reader acked"

    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE scope='inbox' AND user_id='badgeuser3'")


@pytest.mark.asyncio
async def test_bridge_heartbeat_preserves_watcher_last_seen(client, db_pool):
    """MSG-9: the bridge beat must not destroy the watcher's liveness field.

    Two writers, one row. The watcher merges (jsonb_set); the bridge replaces
    metadata wholesale. Before this fix the bridge wiped `watcher_last_seen`
    on every beat — and because the bridge beat rides TOOL CALLS while the
    watcher polls on its own timer, the busiest sessions lost the field
    permanently and advertised themselves as NOT LISTENING while demonstrably
    alive. That inverts the one death signal that survives a session being
    head-down, so it must be pinned.
    """
    ident, proj = "msg9probe", "msg9proj"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='presence' AND user_id=$1", proj)

    # a session heartbeat creates the row
    r = await client.post("/memory/presence", json={
        "identity": ident, "project": proj, "state": "running"})
    assert r.status_code == 200

    # the watcher beats — merges watcher_last_seen in
    r = await client.post("/memory/presence", json={
        "identity": ident, "project": proj, "state": "running", "watcher": True})
    assert r.status_code == 200

    async with db_pool.acquire() as conn:
        seen_before = await conn.fetchval(
            "SELECT metadata->>'watcher_last_seen' FROM memories "
            "WHERE scope='presence' AND user_id=$1 AND key=$2",
            proj, f"presence/{ident}")
    assert seen_before, "watcher beat did not record watcher_last_seen"

    # now the session beats again, as it does on every tool call
    await client.post("/memory/presence", json={
        "identity": ident, "project": proj, "state": "running"})

    async with db_pool.acquire() as conn:
        seen_after = await conn.fetchval(
            "SELECT metadata->>'watcher_last_seen' FROM memories "
            "WHERE scope='presence' AND user_id=$1 AND key=$2",
            proj, f"presence/{ident}")
    assert seen_after == seen_before, (
        "the bridge heartbeat destroyed watcher_last_seen — a live, listening "
        "session now reads as NOT LISTENING, and the busier it is the worse it gets"
    )

    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='presence' AND user_id=$1", proj)


@pytest.mark.asyncio
async def test_action_intent_to_a_dead_recipient_warns_the_sender(client, db_pool):
    """The failure: a peer divided work with a counterparty 42 hours dead.

    The roster would have answered in one call. Nobody made the call, because
    making it is a step you have to remember. The data is one query away at
    the exact moment of the mistake, so the send response is where it belongs.
    """
    ident, proj = "deadpeer", "deadpeerproj"
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE scope='presence' AND user_id=$1", proj)
        await conn.execute("DELETE FROM memories WHERE scope='inbox' AND user_id=$1", ident)

    await client.post("/memory/presence", json={
        "identity": ident, "project": proj, "state": "running"})
    await client.post("/memory/presence", json={
        "identity": ident, "project": proj, "state": "running", "watcher": True})
    # its watcher stops: the positive death signal
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE memories
            SET metadata = jsonb_set(metadata, '{watcher_last_seen}',
                    to_jsonb((NOW() - interval '42 hours')::text), true),
                last_used_at = NOW() - interval '42 hours'
            WHERE scope='presence' AND user_id=$1 AND key=$2
            """,
            proj, f"presence/{ident}")

    r = await client.post("/memory/send", json={
        "to": ident, "from_": "planner", "intent": "action",
        "subject": "you take the second half", "body": "splitting the work"})
    assert r.status_code == 200
    warnings = r.json().get("recipient_warnings")
    assert warnings, (
        "an intent=action message to a 42h-dead recipient reported plain "
        "success — this is the send that cost a peer a turn of duplicated work"
    )
    assert ident in warnings[0]
    assert "presumed-dead" in warnings[0]

    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE scope='presence' AND user_id=$1", proj)
        await conn.execute("DELETE FROM memories WHERE scope='inbox' AND user_id=$1", ident)


@pytest.mark.asyncio
async def test_fyi_to_a_dead_recipient_is_silent(client, db_pool):
    """Queued mail is a FEATURE, not an error — the owner's own correction.

    Sending to a session that is not running is legitimate and frequent; that
    is how a message waits for the next session to start. The distinction is
    PURPOSE, which `intent` already carries, so liveness alone must never
    trigger the warning.
    """
    ident, proj = "quietpeer", "quietpeerproj"
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE scope='presence' AND user_id=$1", proj)
        await conn.execute("DELETE FROM memories WHERE scope='inbox' AND user_id=$1", ident)

    await client.post("/memory/presence", json={
        "identity": ident, "project": proj, "state": "running"})
    await client.post("/memory/presence", json={
        "identity": ident, "project": proj, "state": "running", "watcher": True})
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE memories
            SET metadata = jsonb_set(metadata, '{watcher_last_seen}',
                    to_jsonb((NOW() - interval '42 hours')::text), true),
                last_used_at = NOW() - interval '42 hours'
            WHERE scope='presence' AND user_id=$1 AND key=$2
            """,
            proj, f"presence/{ident}")

    r = await client.post("/memory/send", json={
        "to": ident, "from_": "narrator", "intent": "fyi",
        "subject": "for the record", "body": "no reply needed"})
    assert r.status_code == 200
    assert not r.json().get("recipient_warnings"), (
        "fyi to a dormant session warned — queued mail is the feature, and "
        "warning on it trains the reader to ignore the warning"
    )

    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE scope='presence' AND user_id=$1", proj)
        await conn.execute("DELETE FROM memories WHERE scope='inbox' AND user_id=$1", ident)


@pytest.mark.asyncio
async def test_never_warns_about_an_address_with_no_presence_row(client, db_pool):
    """ABSENT IS NOT DEAD — the conflation behind most of this defect class.

    A session that has never heartbeated has no presence row. Rendering that
    as "dead" would flag every brand-new address, which is exactly the case
    that must stay silent. Enforced by omission: no row, no entry, no warning.
    """
    ident = "neverseenpeer"
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE scope='inbox' AND user_id=$1", ident)

    r = await client.post("/memory/send", json={
        "to": ident, "from_": "planner", "intent": "action",
        "subject": "start when you wake", "body": "queued for a future session"})
    assert r.status_code == 200
    assert not r.json().get("recipient_warnings"), (
        "an address with no presence row was reported as dead — absent is not dead"
    )

    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE scope='inbox' AND user_id=$1", ident)


@pytest.mark.asyncio
async def test_banner_unread_count_is_a_count_not_a_page_size(client, db_pool):
    """The banner reported the size of its own preview window as the total.

    `inbox_banner` fetched `LIMIT preview_limit + 1` and returned `len(msgs)`,
    so "unread" could never exceed 6 no matter how much mail was waiting. A
    session on 130 open messages was told 6 — small enough to read as a real
    answer rather than an obvious truncation, which is what made it dangerous.
    A peer reported seeing three different numbers for one mailbox (banner 6,
    listing 20, digest 130) and concluded it could trust none of them.
    """
    reader = "counter"
    addr = "countbox"
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE scope='inbox' AND user_id=$1", addr)

    for i in range(9):
        r = await client.post("/memory/send", json={
            "to": addr, "from_": "sender", "subject": f"msg {i}", "body": "x"})
        assert r.status_code == 200

    # the banner rides any store/search call
    r = await client.post("/memory/search", json={
        "namespace": "test", "query": "anything", "limit": 1,
        "listen_set": [addr], "reader_identity": reader})
    assert r.status_code == 200
    banner = r.json().get("inbox_banner")
    assert banner is not None, "nine unread messages produced no banner"
    assert banner["unread_count"] == 9, (
        f"banner reported {banner['unread_count']} unread for 9 messages — "
        "this is the preview window size being presented as a total"
    )
    assert len(banner["preview"]) <= 5, "preview must stay short"
    assert banner.get("shown") == len(banner["preview"])

    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE scope='inbox' AND user_id=$1", addr)


@pytest.mark.asyncio
async def test_a_restarted_session_is_not_presumed_dead_by_its_predecessors_watcher(client, db_pool):
    """SEAT-4 REFINEMENT — the defect a power outage found four hours after ship.

    `watcher_last_seen` describes the process that armed that watcher, but it
    survives on a presence row the NEXT generation reclaims through SEAT-9
    continuity. So after a restart the DEAD generation's watcher evidence was
    applied to the LIVE generation's state, and the roster reported a running
    process as presumed-dead. Measured live 2026-07-27: watcher beat 37s before
    the power cut, presence beat four minutes after boot, roster said dead.

    The guard is the NONCE, not a clock — a timestamp comparison has to be
    right about time across a boot, which is when it is least trustworthy and
    exactly when this fires. A new nonce means a process we have not seen, so
    its predecessor's watcher cannot speak for it.
    """
    ident, proj = "restarted", "restartproj"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='presence' AND user_id=$1", proj)

    # generation 1 comes up and arms a watcher
    await client.post("/memory/presence", json={
        "identity": ident, "project": proj, "state": "running",
        "session_nonce": "gen1"})
    await client.post("/memory/presence", json={
        "identity": ident, "project": proj, "state": "running", "watcher": True})

    # the power dies. gen1's watcher beat is now stale — old enough that, left
    # in place, it reads as a positive death signal.
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE memories
            SET metadata = jsonb_set(metadata, '{watcher_last_seen}',
                    to_jsonb((NOW() - interval '3 hours')::text), true)
            WHERE scope='presence' AND user_id=$1 AND key=$2
            """,
            proj, f"presence/{ident}")

    # the box reboots and generation 2 claims the same identity. Its watcher
    # has not armed yet — that happens a few minutes into /startup.
    await client.post("/memory/presence", json={
        "identity": ident, "project": proj, "state": "running",
        "session_nonce": "gen2"})

    r = await client.post("/memory/roster", json={"project": proj})
    entry = next(e for e in r.json()["entries"] if e["identity"] == ident)
    assert entry["watcher_alive"] is None, (
        "the predecessor's watcher beat is still being read as evidence about "
        "the live generation"
    )
    assert entry["presumed_dead"] is False
    assert entry["state"] == "running", (
        "a LIVE session was declared dead by its own predecessor's watcher — "
        "this is what the roster said about a running process seven minutes "
        "after the 2026-07-27 outage"
    )

    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='presence' AND user_id=$1", proj)


@pytest.mark.asyncio
async def test_same_generation_still_preserves_watcher_last_seen(client, db_pool):
    """The generational guard must not undo MSG-9.

    Carrying `watcher_last_seen` forward is correct WITHIN a generation — that
    is the whole of MSG-9, and without it the busiest sessions lose the field
    on every tool call. Only a nonce we have never seen may clear it.
    """
    ident, proj = "samegen", "samegenproj"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='presence' AND user_id=$1", proj)

    await client.post("/memory/presence", json={
        "identity": ident, "project": proj, "state": "running",
        "session_nonce": "gen1"})
    await client.post("/memory/presence", json={
        "identity": ident, "project": proj, "state": "running", "watcher": True})

    async with db_pool.acquire() as conn:
        before = await conn.fetchval(
            "SELECT metadata->>'watcher_last_seen' FROM memories "
            "WHERE scope='presence' AND user_id=$1 AND key=$2",
            proj, f"presence/{ident}")
    assert before

    # the SAME process beats again, as it does on every tool call
    await client.post("/memory/presence", json={
        "identity": ident, "project": proj, "state": "running",
        "session_nonce": "gen1"})

    async with db_pool.acquire() as conn:
        after = await conn.fetchval(
            "SELECT metadata->>'watcher_last_seen' FROM memories "
            "WHERE scope='presence' AND user_id=$1 AND key=$2",
            proj, f"presence/{ident}")
    assert after == before, (
        "the generational guard fired on the same generation and re-introduced "
        "MSG-9 — a live listening session now reads as NOT LISTENING"
    )

    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='presence' AND user_id=$1", proj)


@pytest.mark.asyncio
async def test_roster_corrects_a_corpses_running_using_watcher_truth(client, db_pool):
    """SEAT-4: a dead session never retracts its own 'running'.

    So the roster must correct it — and the ONLY signal licensed to do that is
    the watcher, whose beat rides its own timer rather than tool activity. A
    session head-down in a long call goes quiet without dying (MSG-8), so
    is_stale can never carry this; a watcher that HAS beaten and then stopped
    is a process that exited.
    """
    ident, proj = "corpse", "corpseproj"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='presence' AND user_id=$1", proj)

    await client.post("/memory/presence", json={
        "identity": ident, "project": proj, "state": "running"})
    await client.post("/memory/presence", json={
        "identity": ident, "project": proj, "state": "running", "watcher": True})

    # the session dies: its watcher stops beating, but its last self-report
    # ("running") stands forever because nobody is left to retract it.
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE memories
            SET metadata = jsonb_set(metadata, '{watcher_last_seen}',
                    to_jsonb((NOW() - interval '3 hours')::text), true)
            WHERE scope='presence' AND user_id=$1 AND key=$2
            """,
            proj, f"presence/{ident}")

    r = await client.post("/memory/roster", json={"project": proj})
    entry = next(e for e in r.json()["entries"] if e["identity"] == ident)
    assert entry["state"] == "presumed-dead", (
        f"a corpse still advertises {entry['state']!r} — this is what offered "
        "a human two dead seats to huddle with"
    )
    assert entry["presumed_dead"] is True
    assert entry["reported_state"] == "running", "the original claim must stay visible"

    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='presence' AND user_id=$1", proj)


@pytest.mark.asyncio
async def test_a_session_with_no_watcher_is_never_presumed_dead(client, db_pool):
    """ABSENT IS NOT DEAD — the conflation behind most of this defect class.

    watcher_alive is three-valued. None means no watcher has EVER beaten for
    this identity (older build, or none armed), which is no basis at all.
    Coercing None to False would declare every un-watched session a corpse.
    """
    ident, proj = "nowatcher", "nowatcherproj"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='presence' AND user_id=$1", proj)

    # heartbeats only — no watcher beat has ever been recorded here
    await client.post("/memory/presence", json={
        "identity": ident, "project": proj, "state": "running"})

    r = await client.post("/memory/roster", json={"project": proj})
    entry = next(e for e in r.json()["entries"] if e["identity"] == ident)
    assert entry["watcher_alive"] is None
    assert entry["presumed_dead"] is False
    assert entry["state"] == "running", (
        "a session that never armed a watcher was declared dead — absent is not dead"
    )

    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='presence' AND user_id=$1", proj)
