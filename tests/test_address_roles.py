"""ROLE-1 — owner-assigned roles: one holder at a time, reassignable.

The owner's case: "Hand" is his chief of staff, filled today by one
assistant and perhaps by another product later. Mail to the role must reach
whoever holds it now, a handover must pass the role's open mail to the
successor, and the previous holder keeps its own mail.
"""
import uuid

import pytest
import pytest_asyncio

from server.services import address_roles, mail_signal


def _n():
    t = uuid.uuid4().hex[:6]
    return f"rl-role-{t}", f"rl-a-{t}", f"rl-b-{t}"


async def _cleanup(db_pool, names):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope IN ('inbox','presence') AND "
            "(split_part(user_id,'@',1) = ANY($1::text[]) OR "
            " key = ANY($2::text[]))",
            names, [f"presence/{x}" for x in names])
        await conn.execute(
            "DELETE FROM address_roles WHERE role = ANY($1::text[])", names)
        await conn.execute(
            "DELETE FROM address_role_history WHERE role = ANY($1::text[])", names)
        await conn.execute(
            "DELETE FROM memories WHERE project = ANY($1::text[])", names)
        await conn.execute(
            "DELETE FROM principals WHERE name = ANY($1::text[])", names)
    await address_roles.refresh(force=True)


@pytest_asyncio.fixture
async def names(db_pool):
    n = _n()
    await _cleanup(db_pool, list(n))
    yield n
    await _cleanup(db_pool, list(n))


async def _assign(client, role, holder, status=200):
    r = await client.put(f"/admin/roles/{role}", json={"holder": holder})
    assert r.status_code == status, r.text
    return r.json()


async def _send(client, to, subject, intent="action"):
    r = await client.post("/memory/send", json={
        "to": to, "subject": subject, "body": "b", "intent": intent})
    assert r.status_code == 200, r.text
    return r.json()


async def _inbox(client, me):
    r = await client.post("/memory/inbox", json={
        "listen_set": [me], "reader_identity": me, "unread_only": False})
    return sorted(m["subject"] for m in r.json()["messages"])


@pytest.mark.asyncio
async def test_role_mail_reaches_holder_and_is_stored_under_the_role(
        client, db_pool, names):
    role, a, b = names
    await _assign(client, role, a)
    await _send(client, role, "for the role")
    assert await _inbox(client, a) == ["for the role"]
    async with db_pool.acquire() as conn:
        owner = await conn.fetchval(
            "SELECT user_id FROM memories WHERE scope='inbox' "
            "AND metadata->>'subject' = 'for the role' AND user_id = ANY($1)",
            [role, a])
    assert owner == role
    assert (await client.get(f"/memory/role/{role}")).json()["holder"] == a


@pytest.mark.asyncio
async def test_handover_moves_the_role_mailbox_not_personal_mail(
        client, names):
    role, a, b = names
    await _assign(client, role, a)
    await _send(client, role, "open ask to the role")
    await _send(client, a, "personal to a")
    done = await _assign(client, role, b)
    assert done["previous"] == a and done["holder"] == b
    assert await _inbox(client, b) == ["open ask to the role"]
    assert await _inbox(client, a) == ["personal to a"]
    hist = (await client.get(f"/admin/roles/{role}/history")).json()
    assert [h["holder"] for h in hist] == [a, b]
    assert hist[0]["ended_at"] and not hist[1]["ended_at"]


@pytest.mark.asyncio
async def test_wait_wakes_the_holder_on_role_mail(client, names):
    role, a, b = names
    await _assign(client, role, a)
    await _send(client, role, "wake me")
    w = await client.post("/memory/inbox/wait", json={
        "listen_set": [a], "reader_identity": a, "timeout_seconds": 1,
        "since": "2000-01-01T00:00:00Z"})
    assert [m["subject"] for m in w.json()["messages"]] == ["wake me"]


@pytest.mark.asyncio
async def test_poll_signal_counts_role_mail(client, names):
    role, a, b = names
    await _assign(client, role, a)
    before = await mail_signal.signal_state(a)
    await _send(client, role, "signal me")
    after = await mail_signal.signal_state(a)
    assert after["cursor"] != before["cursor"]
    assert after["pending"] == before["pending"] + 1


@pytest.mark.asyncio
async def test_role_name_may_not_shadow_anything(client, db_pool, names):
    import server.services.principal_service as ps
    role, a, b = names
    await ps.create_principal(name=role, type="agent")
    r = await client.put(f"/admin/roles/{role}", json={"holder": a})
    assert r.status_code == 422
    assert "login (principal) name" in r.json()["detail"]
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM principals WHERE name = $1", role)
    await client.post("/memory/set", json={
        "namespace": "fleet", "scope": "project", "user_id": "claude-code",
        "project": role, "key": "k", "value": "v"})
    r = await client.put(f"/admin/roles/{role}", json={"holder": a})
    assert r.status_code == 422 and "project with memory" in r.json()["detail"]


@pytest.mark.asyncio
async def test_roles_do_not_chain_and_retire_cleanly(client, names):
    role, a, b = names
    await _assign(client, role, a)
    r = await client.put(f"/admin/roles/{b}", json={"holder": role})
    assert r.status_code == 422 and "do not chain" in r.json()["detail"]
    r = await client.delete(f"/admin/roles/{role}")
    assert r.status_code == 200
    assert (await client.get(f"/memory/role/{role}")).status_code == 404
    await _send(client, role, "after retirement")
    assert await _inbox(client, a) == []
