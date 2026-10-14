"""ROOM-MENTION-1 (owner, 2026-10-14): a room post with no @-name is refused
AT SEND TIME. The relay drops such a post and mails back a "NOT POSTED"
letter, which an agent mid-turn never reads — so it believes it spoke.
Measured the same morning: two agents lost four room posts between them.
Only the presence of a member @-name is checked here; the relay still
validates the name against the room."""
import pytest

from server.config import settings


async def _cleanup(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='inbox' AND value LIKE 'rm1-%'")


def _post(body, **extra):
    return {"to": "owner", "body": body, "subject": "s", "from_": "rm1-agent",
            "thread_id": "huddle/rm1room", "intent": "fyi", **extra}


@pytest.mark.asyncio
async def test_room_post_without_a_mention_is_refused_in_the_same_call(client, db_pool, monkeypatch):
    monkeypatch.setattr(settings, "owner_principal_name", "owner")
    await _cleanup(db_pool)
    try:
        r = await client.post("/memory/send", json=_post("rm1-no mention here"))
        assert r.status_code == 422, r.text
        d = r.json()["detail"]
        assert d.startswith("NOT SENT") and "@all" in d
        assert "rm1-no mention here" in d     # the agent gets its text back
        # A name quoted in backticks / double quotes is talked ABOUT; the
        # relay ignores it, so it does not satisfy the check.
        r = await client.post("/memory/send", json=_post("rm1-ask `@PM@projx` later"))
        assert r.status_code == 422, r.text
        r = await client.post("/memory/send", json=_post('rm1-the "@all" line'))
        assert r.status_code == 422, r.text
        # An e-mail address is not a mention.
        r = await client.post("/memory/send", json=_post("rm1-mail ops@example.com"))
        assert r.status_code == 422, r.text
        async with db_pool.acquire() as conn:
            n = await conn.fetchval(
                "SELECT count(*) FROM memories WHERE scope='inbox' AND value LIKE 'rm1-%'")
        assert n == 0                          # nothing was stored
    finally:
        await _cleanup(db_pool)


@pytest.mark.asyncio
async def test_room_post_with_a_member_mention_goes_through(client, db_pool, monkeypatch):
    monkeypatch.setattr(settings, "owner_principal_name", "owner")
    await _cleanup(db_pool)
    try:
        for body in ["rm1-@all hello", "rm1- @PM@projx done",
                     "rm1-(@admin@hosta) see above",
                     # owner-only lines are valid: DECIDE/COMPLETE page him
                     "rm1-@owner DECIDE: A or B?"]:
            r = await client.post("/memory/send", json=_post(body))
            assert r.status_code == 200, (body, r.text)
    finally:
        await _cleanup(db_pool)


@pytest.mark.asyncio
async def test_not_checked_outside_room_posts(client, db_pool, monkeypatch):
    """DMs, non-huddle threads, lifecycle letters, mail to a peer on a huddle
    thread (HUD-ROUTE-1 warns about that one), and an unset owner."""
    monkeypatch.setattr(settings, "owner_principal_name", "owner")
    monkeypatch.setattr(settings, "huddle_fanout_refusal_enabled", False)
    await _cleanup(db_pool)
    try:
        for payload in [
            {"to": "owner"},
            {"to": "owner", "thread_id": "inbox/rm1-thread"},
            {"to": "owner", "thread_id": "huddle/rm1room", "huddle_lifecycle": True},
            {"to": "rm1-peer", "thread_id": "huddle/rm1room"},
        ]:
            r = await client.post("/memory/send", json={
                "body": "rm1-plain", "subject": "s", "from_": "rm1-agent",
                "intent": "fyi", **payload})
            assert r.status_code == 200, (payload, r.text)
        monkeypatch.setattr(settings, "owner_principal_name", "")
        r = await client.post("/memory/send", json=_post("rm1-unset owner"))
        assert r.status_code == 200, r.text
    finally:
        await _cleanup(db_pool)
