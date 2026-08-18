"""Band D 10d: the huddle-fanout letter class is refused at the door.

Flag-gated (ON only after 10c). The refusal triple — owner principal +
huddle/* thread + non-owner recipient — is the relay's fan-out write and
NOTHING else: over-refusal is the failure mode (the audit lock), because
the same shape minus any one element is how agents speak, how the owner
DMs, and how ordinary mail works.

The test client runs in legacy no-auth mode (principal None), so the
owner-principal leg is exercised through the service function contract:
these tests assert the FLAG-OFF passthrough and every PRESERVED leg; the
refusal triple itself is asserted at the unit boundary via a monkeypatched
principal.
"""

import pytest

from server.config import settings


@pytest.mark.asyncio
async def test_flag_off_is_byte_identical(client, db_pool, monkeypatch):
    monkeypatch.setattr(settings, "huddle_fanout_refusal_enabled", False)
    r = await client.post("/memory/send", json={
        "to": "s10d-a", "body": "b", "subject": "s", "from_": "x",
        "thread_id": "huddle/s10d"})
    assert r.status_code == 200
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='inbox' AND user_id LIKE 's10d%'")


@pytest.mark.asyncio
async def test_preserved_legs_with_flag_on(client, db_pool, monkeypatch):
    """Anonymous/non-admin senders (the agent ingest leg), non-huddle
    threads, and huddle-threaded mail from non-owners all pass untouched
    even with the flag ON — the triple requires the OWNER principal."""
    monkeypatch.setattr(settings, "huddle_fanout_refusal_enabled", True)
    for body in [
        {"to": "ixanadu-probe10d", "body": "utterance", "subject": "s",
         "from_": "agent-claude-2", "thread_id": "huddle/s10d"},  # ingest leg
        {"to": "s10d-b", "body": "dm", "subject": "s", "from_": "x"},  # no thread
        {"to": "s10d-c", "body": "b", "subject": "s", "from_": "x",
         "thread_id": "inbox/ordinary-thread"},                    # non-huddle
    ]:
        r = await client.post("/memory/send", json=body)
        assert r.status_code == 200, (body, r.text[:120])
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='inbox' AND user_id LIKE 's10d%'")
        await conn.execute(
            "DELETE FROM memories WHERE scope='inbox' AND user_id = 'ixanadu-probe10d'")


@pytest.mark.asyncio
async def test_owner_fanout_triple_is_refused_flag_on(
        client, db_pool, monkeypatch):
    """The refusal triple, exercised by patching the resolved principal to
    an admin (the router consults get_current_principal)."""
    monkeypatch.setattr(settings, "huddle_fanout_refusal_enabled", True)
    import server.routers.memory as mem_router
    monkeypatch.setattr(
        mem_router, "get_current_principal",
        lambda request: {"name": "ixanadu", "is_admin": True})
    # Fan-out shape: owner principal, huddle thread, non-owner recipient.
    r = await client.post("/memory/send", json={
        "to": "s10d-victim", "body": "fanout copy", "subject": "s",
        "from_": "ixanadu", "thread_id": "huddle/s10d"})
    assert r.status_code == 409
    assert "wake" in r.json()["detail"]
    # Same principal, same thread, recipient IS the owner: allowed (source copy).
    r = await client.post("/memory/send", json={
        "to": "ixanadu", "body": "self", "subject": "s",
        "from_": "ixanadu", "thread_id": "huddle/s10d"})
    assert r.status_code == 200
    # Same principal, no huddle thread: allowed (a true DM).
    r = await client.post("/memory/send", json={
        "to": "s10d-dm", "body": "real dm", "subject": "s", "from_": "ixanadu"})
    assert r.status_code == 200
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='inbox' AND user_id LIKE 's10d%'")
        await conn.execute(
            "DELETE FROM memories WHERE scope='inbox' AND user_id = 'ixanadu' "
            "AND value = 'self'")


@pytest.mark.asyncio
async def test_lifecycle_letters_and_host_qualified_owner_pass(
        client, db_pool, monkeypatch):
    """The two audit locks: relay-declared lifecycle letters (kickoff/
    close/add) stay mail even under the triple; the owner exemption covers
    ixanadu@host, so the source/ingest leg lives host-qualified too."""
    monkeypatch.setattr(settings, "huddle_fanout_refusal_enabled", True)
    import server.routers.memory as mem_router
    monkeypatch.setattr(
        mem_router, "get_current_principal",
        lambda request: {"name": "ixanadu", "is_admin": True})
    # Kickoff shape: owner + huddle thread + non-owner recipient + lifecycle.
    r = await client.post("/memory/send", json={
        "to": "s10d-newmember", "body": "you are in a room", "subject": "k",
        "from_": "ixanadu", "thread_id": "huddle/s10d",
        "huddle_lifecycle": True})
    assert r.status_code == 200, r.text[:200]
    # Host-qualified owner recipient: allowed (bare/host split).
    r = await client.post("/memory/send", json={
        "to": "ixanadu@macmini", "body": "source", "subject": "s",
        "from_": "ixanadu", "thread_id": "huddle/s10d"})
    assert r.status_code == 200
    # The bare triple without the lifecycle bit still refuses.
    r = await client.post("/memory/send", json={
        "to": "s10d-victim2", "body": "fanout", "subject": "s",
        "from_": "ixanadu", "thread_id": "huddle/s10d"})
    assert r.status_code == 409
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='inbox' AND user_id LIKE 's10d%'")
        await conn.execute(
            "DELETE FROM memories WHERE scope='inbox' AND user_id = 'ixanadu@macmini' "
            "AND value = 'source'")
