"""Step 8: the project registry — the address tree's verifiable root.

A project registers on first contact via the claim path; projects that
predate the registry are listed from their seat rows and register
organically. Typo detection rides the send path's existing warning channel:
warn, never reject (ADDR-2 doctrine).
"""

import pytest

from server.services.memory_service import PRESENCE_NAMESPACE

PROJ = "regrootproj"


async def _clear(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope IN ('seat', 'project-root') "
            "AND project LIKE $1", f"{PROJ}%")
        await conn.execute(
            "DELETE FROM memories WHERE scope = 'inbox' AND user_id LIKE $1",
            f"{PROJ}%")


@pytest.mark.asyncio
async def test_claim_registers_the_project_root(client, db_pool):
    await _clear(db_pool)
    r = await client.post("/session/claim", json={
        "session_key": "rootclaimer", "project": PROJ, "provider": "claude"})
    assert r.status_code == 200

    r = await client.get("/session/projects")
    assert r.status_code == 200
    entry = next(p for p in r.json()["projects"] if p["project"] == PROJ)
    assert entry["registered"] is True
    assert entry["dormant"] is False
    assert entry["last_active"] is not None
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_observed_only_project_is_listed_unregistered(client, db_pool):
    """A project that predates the registry (seat rows, no registry row)
    is listed from day one — 'every known project' needs no backfill."""
    await _clear(db_pool)
    ghost = f"{PROJ}ghost"
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO memories (namespace, key, value, scope, user_id,
                                  project, tags, tags_search, metadata)
            VALUES ($1, $2, 'seat', 'seat', 'global', $3, '', '', '{}'::jsonb)
            """,
            PRESENCE_NAMESPACE, f"seat/{ghost}-claude-2", ghost,
        )
    r = await client.get("/session/projects")
    entry = next(p for p in r.json()["projects"] if p["project"] == ghost)
    assert entry["registered"] is False
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope = 'seat' AND project = $1", ghost)


@pytest.mark.asyncio
async def test_unknown_root_send_draws_advisory_but_delivers(client, db_pool):
    await _clear(db_pool)
    r = await client.post("/memory/send", json={
        "to": "definitelynotaproject-claude-9", "body": "b", "subject": "s",
        "from_": "someone"})
    assert r.status_code == 200, "warn, never reject — delivery must succeed"
    warns = r.json().get("recipient_warnings") or []
    assert any("no registered project roots" in w for w in warns), warns
    # cleanup the delivered probe row
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope = 'inbox' AND user_id = $1",
            "definitelynotaproject-claude-9")


@pytest.mark.asyncio
async def test_known_root_person_exempt_and_channel_stay_silent(
        client, db_pool):
    await _clear(db_pool)
    # Register the root via a claim, then mail a deep address under it.
    await client.post("/session/claim", json={
        "session_key": "rootclaimer2", "project": PROJ, "provider": "claude"})
    person = "regrootperson"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO principals (name, type) VALUES ($1, 'human') "
            "ON CONFLICT (name) DO UPDATE SET type='human', active=TRUE",
            person)

    for to in (f"{PROJ}-grok-4", person, "admin@webone", "#regrootchan"):
        r = await client.post("/memory/send", json={
            "to": to, "body": "b", "subject": "s", "from_": "someone"})
        assert r.status_code == 200
        warns = r.json().get("recipient_warnings") or []
        assert not any("no registered project roots" in w for w in warns), (
            to, warns)

    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM principals WHERE name = $1", person)
        for to in (f"{PROJ}-grok-4", person, "admin@webone", "#regrootchan"):
            await conn.execute(
                "DELETE FROM memories WHERE scope='inbox' AND user_id=$1", to)
    await _clear(db_pool)
