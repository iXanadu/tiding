"""Band D 10a: the wake primitive — a wake is not a letter (O6).

TTL wake rows: never inbox-served, never popped (shared listen_sets have
several live waiters — each dedupes), self-echo filtered, from_principal
server-stamped, scope rejected on every generic memory path.
"""

import pytest


@pytest.mark.asyncio
async def test_wake_roundtrip_never_touches_the_inbox(client, db_pool):
    r = await client.post("/memory/send", json={  # baseline: inbox works here
        "to": "wakeprobe-x", "body": "b", "subject": "s", "from_": "peer"})
    assert r.status_code == 200
    r = await client.post("/memory/wake", json={
        "to": ["wakeprobe-a", "wakeprobe-b"], "ref": "huddle/waketest",
        "note": "look at the room", "from_": "wakeprobe-sender"})
    assert r.status_code == 200
    ids = r.json()["ids"]
    assert len(ids) == 2

    # Served on the poll, with the server-stamped principal.
    r = await client.post("/memory/wake/poll", json={
        "listen_set": ["wakeprobe-a"], "reader_identity": "wakeprobe-a@x",
        "timeout_seconds": 0})
    wakes = r.json()["wakes"]
    assert len(wakes) == 1
    assert wakes[0]["ref"] == "huddle/waketest"
    # Amendment 4: the field is server-controlled. The test client runs in
    # legacy no-auth mode, so the stamped value is honestly None here — the
    # stamping path is the same one mail's from_principal rides (auth-mode
    # covered there); what this asserts is that the field EXISTS and is
    # never client-supplied (WakeRequest has no such field to spoof).
    assert "from_principal" in wakes[0]

    # NEVER a letter: the inbox shows nothing for the woken address.
    r = await client.post("/memory/inbox", json={
        "listen_set": ["wakeprobe-a"], "reader_identity": "wakeprobe-a@x",
        "unread_only": False})
    assert r.json()["messages"] == []

    # No pop (amendment 2): a second waiter on the same address still sees it.
    r = await client.post("/memory/wake/poll", json={
        "listen_set": ["wakeprobe-a"], "reader_identity": "sibling@x",
        "timeout_seconds": 0})
    assert len(r.json()["wakes"]) == 1

    # Self-echo filtered like mail (amendment 6).
    r = await client.post("/memory/wake/poll", json={
        "listen_set": ["wakeprobe-a"], "reader_identity": "wakeprobe-sender",
        "timeout_seconds": 0})
    assert r.json()["wakes"] == []

    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope IN ('wake','inbox') "
            "AND user_id LIKE 'wakeprobe%'")


@pytest.mark.asyncio
async def test_expired_wake_is_not_served(client, db_pool):
    """Amendment 3: served only inside TTL — physical delete is the hourly
    cleanup's business, so the verify is not-SERVED, never row-gone."""
    r = await client.post("/memory/wake", json={
        "to": "wakeprobe-old", "ref": "huddle/old", "from_": "p"})
    wid = r.json()["ids"][0]
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE memories SET expires_at = NOW() - INTERVAL '1 second' "
            "WHERE key = $1", wid)
    r = await client.post("/memory/wake/poll", json={
        "listen_set": ["wakeprobe-old"], "timeout_seconds": 0})
    assert r.json()["wakes"] == []
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='wake' AND user_id LIKE 'wakeprobe%'")


@pytest.mark.asyncio
async def test_wait_endpoint_surfaces_wakes_additively(client, db_pool):
    from datetime import datetime, timedelta, timezone
    t0 = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    await client.post("/memory/wake", json={
        "to": "wakeprobe-wait", "ref": "huddle/w", "from_": "p"})
    # `since` is the caller's cursor, same forward-watcher semantics as
    # mail: a looping harness passes its last-seen timestamp and catches
    # wakes that landed between waits.
    r = await client.post("/memory/inbox/wait", json={
        "listen_set": ["wakeprobe-wait"], "reader_identity": "wakeprobe-wait@x",
        "timeout_seconds": 1, "since": t0})
    body = r.json()
    assert body["status"] == "ok"
    assert body["messages"] == []
    assert len(body["wakes"]) == 1
    assert "not a letter" in (body["guidance"] or "")
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='wake' AND user_id LIKE 'wakeprobe%'")


@pytest.mark.asyncio
async def test_wake_scope_is_rejected_on_every_generic_path(client):
    """Amendment 5: exclusion by predicate. Ephemeral wire is not memory."""
    for path, body in [
        ("/memory/set", {"namespace": "fleet", "key": "k", "value": "v",
                         "scope": "wake"}),
        ("/memory/forget", {"namespace": "fleet", "key": "k", "scope": "wake"}),
        ("/memory/get", {"namespace": "fleet", "key": "k", "scope": "wake"}),
        ("/memory/search", {"namespace": "fleet", "query": "q", "scope": "wake"}),
        ("/memory/keys", {"namespace": "fleet", "scope": "wake"}),
    ]:
        r = await client.post(path, json=body)
        assert r.status_code == 400, (path, r.status_code, r.text[:120])


@pytest.mark.asyncio
async def test_self_wake_filters_bare_and_host_qualified_forms(
        client, db_pool):
    """Amendment 6 completed: a speaker stamps from_=<seat> while its
    watcher reads as <seat>@<host> — both forms are self."""
    await client.post("/memory/wake", json={
        "to": "wakeprobe-self", "ref": "huddle/self",
        "from_": "wakeprobe-x"})
    r = await client.post("/memory/wake/poll", json={
        "listen_set": ["wakeprobe-self"],
        "reader_identity": "wakeprobe-x@somehost", "timeout_seconds": 0})
    assert r.json()["wakes"] == [], "host-qualified reader must not self-wake"
    # A genuinely different reader still sees it.
    r = await client.post("/memory/wake/poll", json={
        "listen_set": ["wakeprobe-self"],
        "reader_identity": "wakeprobe-y@somehost", "timeout_seconds": 0})
    assert len(r.json()["wakes"]) == 1
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='wake' AND user_id LIKE 'wakeprobe%'")


@pytest.mark.asyncio
async def test_wake_carries_relayed_from_and_self_echo_filters_on_it(client, db_pool):
    """RELAY-1 (wake half): a relayed wake declares its AUTHOR in the envelope,
    served beside the server-stamped from_principal; the author's own watcher
    treats it as self-echo even when the relay's `from_` label differs."""
    r = await client.post("/memory/wake", json={
        "to": ["wakeprobe-r1", "wakeprobe-r2"], "ref": "huddle/relaytest",
        "note": "author-seat: said a thing", "from_": "relay-label",
        "relayed_from": "Author-Seat"})
    assert r.status_code == 200, r.text
    r = await client.post("/memory/wake/poll", json={
        "listen_set": ["wakeprobe-r1"], "reader_identity": "wakeprobe-r1@x",
        "timeout_seconds": 0})
    wakes = r.json()["wakes"]
    assert len(wakes) == 1
    assert wakes[0]["relayed_from"] == "author-seat"
    assert wakes[0]["from_"] == "relay-label"
    assert "from_principal" in wakes[0]
    # The declared author does not wake itself off its own relayed line.
    r = await client.post("/memory/wake/poll", json={
        "listen_set": ["wakeprobe-r2"], "reader_identity": "author-seat@x",
        "timeout_seconds": 0})
    assert r.json()["wakes"] == []
    # A direct wake (no relay) serves relayed_from=None — absent, not faked.
    r = await client.post("/memory/wake", json={
        "to": "wakeprobe-r3", "ref": "huddle/relaytest", "from_": "direct"})
    r = await client.post("/memory/wake/poll", json={
        "listen_set": ["wakeprobe-r3"], "reader_identity": "wakeprobe-r3@x",
        "timeout_seconds": 0})
    assert r.json()["wakes"][0]["relayed_from"] is None
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope IN ('wake','inbox') "
            "AND user_id LIKE 'wakeprobe%'")
