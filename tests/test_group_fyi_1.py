"""GROUP-FYI-1 (owner GO 2026-09-25): a group send (more than one recipient)
with NO intent is stored as fyi, so it wakes nobody; the receipt says so.
Explicit intent always wins. Exempt: the owner principal's own sends and the
relay's huddle lifecycle letters. A single-recipient send is unchanged."""
import pytest

from server.config import settings

NOTE = "the group default"


async def _cleanup(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='inbox' AND user_id LIKE 'gf1%'")


async def _intent_seen_by(client, who):
    resp = await client.post("/memory/inbox", json={
        "listen_set": [who], "reader_identity": f"{who}@m", "unread_only": False,
        "limit": 50})
    return {m["body"]: m["intent"] for m in resp.json()["messages"]}


@pytest.mark.asyncio
async def test_group_send_without_intent_is_fyi_and_says_so(client, db_pool):
    await _cleanup(db_pool)
    try:
        r = await client.post("/memory/send", json={
            "to": ["gf1-a", "gf1-b"], "body": "gf1-group", "from_": "gf1-s"})
        assert r.status_code == 200, r.text
        assert any(NOTE in c for c in (r.json().get("cost_warnings") or []))
        for who in ("gf1-a", "gf1-b"):
            assert (await _intent_seen_by(client, who))["gf1-group"] == "fyi"
    finally:
        await _cleanup(db_pool)


@pytest.mark.asyncio
async def test_explicit_intent_on_a_group_send_wins(client, db_pool):
    await _cleanup(db_pool)
    try:
        r = await client.post("/memory/send", json={
            "to": ["gf1-a", "gf1-b"], "body": "gf1-ask", "from_": "gf1-s",
            "intent": "action"})
        assert r.status_code == 200, r.text
        assert not any(NOTE in c for c in (r.json().get("cost_warnings") or []))
        assert (await _intent_seen_by(client, "gf1-a"))["gf1-ask"] == "action"
    finally:
        await _cleanup(db_pool)


@pytest.mark.asyncio
async def test_single_recipient_send_keeps_the_waking_default(client, db_pool):
    await _cleanup(db_pool)
    try:
        r = await client.post("/memory/send", json={
            "to": "gf1-a", "body": "gf1-dm", "from_": "gf1-s"})
        assert r.status_code == 200, r.text
        assert (await _intent_seen_by(client, "gf1-a"))["gf1-dm"] is None
    finally:
        await _cleanup(db_pool)


@pytest.mark.asyncio
async def test_owner_group_send_still_wakes(client, db_pool, monkeypatch):
    monkeypatch.setattr(settings, "owner_principal_name", "owner")
    import server.routers.memory as mem_router
    monkeypatch.setattr(mem_router, "get_current_principal",
                        lambda request: {"name": "owner", "is_admin": True})
    await _cleanup(db_pool)
    try:
        r = await client.post("/memory/send", json={
            "to": ["gf1-a", "gf1-b"], "body": "gf1-owner", "from_": "owner"})
        assert r.status_code == 200, r.text
        assert (await _intent_seen_by(client, "gf1-a"))["gf1-owner"] is None
    finally:
        await _cleanup(db_pool)


@pytest.mark.asyncio
async def test_huddle_lifecycle_letters_are_exempt(client, db_pool, monkeypatch):
    monkeypatch.setattr(settings, "huddle_fanout_refusal_enabled", False)
    await _cleanup(db_pool)
    try:
        r = await client.post("/memory/send", json={
            "to": ["gf1-a", "gf1-b"], "body": "gf1-kickoff", "from_": "gf1-s",
            "thread_id": "huddle/gf1room", "huddle_lifecycle": True})
        assert r.status_code == 200, r.text
        assert (await _intent_seen_by(client, "gf1-a"))["gf1-kickoff"] is None
    finally:
        await _cleanup(db_pool)
