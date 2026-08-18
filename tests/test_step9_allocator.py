"""Step 9 engram half: ONE skip ladder (ADDR-REG-1) + root reservation.

The ladder is a single pure function consulted by both the allocator and
the register — the equivalence that used to be maintained by hand. Roots
(bare project strings, ANY known root — registered or observed-only, the
audit amendment) are never grantable as seats.
"""

import pytest

from server.services.memory_service import PRESENCE_NAMESPACE
from server.services.session_registry import (
    SEAT_GRACE_SECONDS,
    SEAT_LIVE_SECONDS,
    allocation_decision,
)

PROJ = "step9proj"


async def _clear(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope IN ('seat','project-root') "
            "AND project LIKE 'step9%'")
        await conn.execute(
            "DELETE FROM memories WHERE scope='inbox' AND user_id LIKE 'step9%'")


def test_ladder_order_is_the_documented_one():
    """The matrix: every rung, in order — the single copy both consumers
    share. Root > lane > (rowless: mail/free) > live > grace > mail >
    presence > free."""
    base = dict(root=False, lane=False, age=None, holds_mail=False,
                presence_fresh=False, last_used_at=None)
    def d(**kw):
        return allocation_decision(**{**base, **kw})

    assert d(root=True, lane=True, holds_mail=True)["reason"] == "reserved-root"
    assert d(lane=True, holds_mail=True)["reason"] == "reserved-lane"
    assert d(holds_mail=True)["reason"] == "mail-parked"          # rowless
    assert d()["would_skip"] is False                             # rowless free
    assert d(age=10, holds_mail=True)["reason"] == "live-holder"
    assert d(age=SEAT_LIVE_SECONDS + 1,
             holds_mail=True)["reason"] == "grace-window"
    assert d(age=SEAT_GRACE_SECONDS + 1,
             holds_mail=True)["reason"] == "mail-parked"
    assert d(age=SEAT_GRACE_SECONDS + 1,
             presence_fresh=True)["reason"] == "presence-fresh"
    assert d(age=SEAT_GRACE_SECONDS + 1)["would_skip"] is False   # takeover


@pytest.mark.asyncio
async def test_runtime_rename_onto_known_root_is_refused_loud(
        client, db_pool, monkeypatch):
    """take_seat("<any known root>") — the squat hole — is refused, even for
    an OBSERVED-ONLY root (the audit amendment)."""
    from server.config import settings
    monkeypatch.setattr(settings, "lane_reservation_enabled", True)
    await _clear(db_pool)
    obs = "step9observed"
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO memories (namespace, key, value, scope, user_id,
                                  project, tags, tags_search, metadata)
            VALUES ($1, $2, 'seat', 'seat', 'global', $3, '', '', '{}'::jsonb)
            """,
            PRESENCE_NAMESPACE, f"seat/{obs}-claude-2", obs)

    r = await client.post("/session/claim", json={
        "session_key": "renamer", "project": PROJ, "provider": "claude",
        "session_nonce": "rn1"})
    held = r.json()["seat"]
    r = await client.post("/session/claim", json={
        "session_key": "renamer", "project": PROJ, "provider": "claude",
        "session_nonce": "rn1", "runtime_seat": True, "preferred_seat": obs})
    body = r.json()
    assert body["seat"] == held, "rename must not move onto a root"
    assert body.get("warning") and "root" in body["warning"], body
    # Own bare root refused the same way.
    r = await client.post("/session/claim", json={
        "session_key": "renamer", "project": PROJ, "provider": "claude",
        "session_nonce": "rn1", "runtime_seat": True, "preferred_seat": PROJ})
    body = r.json()
    assert body["seat"] == held
    assert body.get("warning") and "root" in body["warning"], body
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_launch_preference_for_bare_root_demotes_loud(
        client, db_pool, monkeypatch):
    from server.config import settings
    monkeypatch.setattr(settings, "lane_reservation_enabled", True)
    await _clear(db_pool)
    r = await client.post("/session/claim", json={
        "session_key": "rootwanter", "project": PROJ, "provider": "claude",
        "preferred_seat": PROJ})
    body = r.json()
    assert body["seat"] != PROJ
    assert body["seat"].startswith(f"{PROJ}-claude")
    assert body.get("warning") and "root_reserved" in body["warning"], body
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_register_serves_reserved_root_on_legacy_bare_row(
        client, db_pool, monkeypatch):
    """The seat/engram cursor-corpse class: a legacy bare-root seat row must
    read reserved-root, whatever its age — never claimable."""
    from server.config import settings
    monkeypatch.setattr(settings, "lane_reservation_enabled", True)
    await _clear(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO memories (namespace, key, value, scope, user_id,
                                  project, tags, tags_search, metadata,
                                  last_used_at)
            VALUES ($1, $2, 'seat', 'seat', 'global', $3, '', '',
                    '{}'::jsonb, NOW() - INTERVAL '30 days')
            """,
            PRESENCE_NAMESPACE, f"seat/{PROJ}", PROJ)
    r = await client.get("/session/addresses", params={"project": PROJ})
    entry = {e["address"]: e for e in r.json()["entries"]}[PROJ]
    assert entry["allocation"]["reason"] == "reserved-root"
    assert entry["allocation"]["would_skip"] is True
    await _clear(db_pool)
