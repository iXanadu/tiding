"""HUD-ROUTE-1 (2026-08-22): the huddle relay ingests the OWNER's inbox and
fans out by thread_id, so a send carrying `huddle/<id>` but addressed to anyone
other than the owner never enters the room — and used to return a clean success
receipt. Now the receipt carries a recipient_warnings line naming the miss and
the correct action. Warn, never reject (ADDR-2). Excluded: the owner principal
itself (the relay), lifecycle letters (kickoff/close/add — addressed to
participants by design), host-qualified owner addresses, non-huddle threads."""
import pytest

from server.config import settings

HUD = "will NOT enter the room"


async def _cleanup(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='inbox' AND (user_id LIKE 'hr1%' "
            "OR (user_id IN ('ixanadu','ixanadu@macmini') AND value LIKE 'hr1-%'))")


def _hud(r):
    return [w for w in (r.json().get("recipient_warnings") or []) if HUD in w]


@pytest.mark.asyncio
async def test_huddle_threaded_send_to_a_peer_warns_and_names_the_action(client, db_pool, monkeypatch):
    monkeypatch.setattr(settings, "owner_principal_name", "ixanadu")
    monkeypatch.setattr(settings, "huddle_fanout_refusal_enabled", False)
    await _cleanup(db_pool)
    try:
        r = await client.post("/memory/send", json={
            "to": "hr1-peer", "body": "hr1-x", "subject": "s", "from_": "hr1-sender",
            "thread_id": "huddle/hr1room", "intent": "fyi"})
        assert r.status_code == 200, r.text          # warn, never reject
        w = _hud(r)
        assert len(w) == 1, r.json().get("recipient_warnings")
        assert "huddle/hr1room" in w[0] and "hr1-peer" in w[0] and "ixanadu" in w[0]
        assert "memory_reply to the huddle kickoff" in w[0]
        # Fan-out: one line per offending recipient, none for the owner.
        r = await client.post("/memory/send", json={
            "to": ["hr1-peer", "hr1-peer2", "ixanadu"], "body": "hr1-y", "subject": "s",
            "from_": "hr1-sender", "thread_id": "huddle/hr1room", "intent": "fyi"})
        assert r.status_code == 200, r.text
        w = _hud(r)
        assert len(w) == 2 and all("ixanadu, this" not in x for x in w)
    finally:
        await _cleanup(db_pool)


@pytest.mark.asyncio
async def test_no_warning_for_owner_recipient_non_huddle_thread_lifecycle_or_unset_owner(client, db_pool, monkeypatch):
    monkeypatch.setattr(settings, "owner_principal_name", "ixanadu")
    monkeypatch.setattr(settings, "huddle_fanout_refusal_enabled", False)
    await _cleanup(db_pool)
    try:
        for body, payload in [
            ("hr1-owner", {"to": "ixanadu", "thread_id": "huddle/hr1room"}),
            ("hr1-owner-host", {"to": "ixanadu@macmini", "thread_id": "huddle/hr1room"}),
            ("hr1-dm", {"to": "hr1-peer", "thread_id": "inbox/some-thread"}),
            ("hr1-nothread", {"to": "hr1-peer"}),
            ("hr1-lifecycle", {"to": "hr1-peer", "thread_id": "huddle/hr1room", "huddle_lifecycle": True}),
        ]:
            r = await client.post("/memory/send", json={
                "body": body, "subject": "s", "from_": "hr1-sender", "intent": "fyi", **payload})
            assert r.status_code == 200, r.text
            assert _hud(r) == [], (body, r.json().get("recipient_warnings"))
        # Owner principal unset: the store cannot judge, so it stays silent.
        monkeypatch.setattr(settings, "owner_principal_name", "")
        r = await client.post("/memory/send", json={
            "to": "hr1-peer", "body": "hr1-unset", "subject": "s", "from_": "hr1-sender",
            "thread_id": "huddle/hr1room", "intent": "fyi"})
        assert r.status_code == 200 and _hud(r) == []
    finally:
        await _cleanup(db_pool)


@pytest.mark.asyncio
async def test_owner_principal_itself_is_not_warned(client, db_pool, monkeypatch):
    """The relay (owner token) knows the routing; its sends are not nagged."""
    monkeypatch.setattr(settings, "owner_principal_name", "ixanadu")
    monkeypatch.setattr(settings, "huddle_fanout_refusal_enabled", False)
    import server.routers.memory as mem_router
    monkeypatch.setattr(mem_router, "get_current_principal",
                        lambda request: {"name": "ixanadu", "is_admin": True})
    await _cleanup(db_pool)
    try:
        r = await client.post("/memory/send", json={
            "to": "hr1-peer", "body": "hr1-relay", "subject": "s", "from_": "ixanadu",
            "thread_id": "huddle/hr1room", "intent": "fyi"})
        assert r.status_code == 200, r.text
        assert _hud(r) == []
    finally:
        await _cleanup(db_pool)
