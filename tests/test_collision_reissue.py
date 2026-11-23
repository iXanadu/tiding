"""Seat presence across evidence-backed release and reissue."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio


PROJECT = "reissuetest"
OLD_NONCE = "departing-process"
NEW_NONCE = "remaining-process"


@pytest_asyncio.fixture(autouse=True)
async def clear_registry(db_pool):
    async def clear():
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM memories
                WHERE (scope IN ('seat', 'death', 'lane_cursor') AND project = $1)
                   OR (scope = 'presence' AND user_id = $1)
                """,
                PROJECT,
            )

    await clear()
    yield
    await clear()


async def claim(client, key, nonce):
    response = await client.post("/session/claim", json={
        "project": PROJECT, "provider": "claude",
        "session_key": key, "session_nonce": nonce,
    })
    assert response.status_code == 200, response.text
    return response.json()["seat"]


async def roster_state(client, seat):
    response = await client.post("/memory/roster", json={"project": PROJECT})
    assert response.status_code == 200, response.text
    entry = next(e for e in response.json()["entries"] if e["identity"] == seat)
    return entry["live_sessions"], entry["collision"]


async def assert_roster(client, seat, live):
    assert await roster_state(client, seat) == (live, live > 1)


async def beat(client, seat, nonce, live):
    response = await client.post("/memory/presence", json={
        "identity": seat, "project": PROJECT, "state": "running",
        "provider": "claude", "session_nonce": nonce,
    })
    assert response.status_code == 200, response.text
    collision = {"live_sessions": live, "providers": ["claude"]} if live > 1 else None
    assert response.json()["collision"] == collision
    await assert_roster(client, seat, live)


async def release(client, seat, fields, *, key="old-key"):
    response = await client.post("/session/release", json={
        "project": PROJECT, "session_key": key, **fields,
    })
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ok", "released": seat}


@pytest.mark.parametrize("evidence", ["performed", "observed", None])
@pytest.mark.parametrize("fenced", [True, False], ids=["fenced", "unfenced"])
@pytest.mark.asyncio
async def test_certain_release_reissues_without_collision(client, evidence, fenced):
    seat = await claim(client, "old-key", OLD_NONCE)
    await beat(client, seat, OLD_NONCE, 1)
    fields = {"evidence": evidence} if evidence is not None else {}
    if fenced:
        fields["session_nonce"] = OLD_NONCE
    await release(client, seat, fields)
    after_release = await roster_state(client, seat)
    assert await claim(client, "new-key", NEW_NONCE) == seat
    before_beat = await roster_state(client, seat)
    await beat(client, seat, NEW_NONCE, 1)
    assert after_release == before_beat == (0, False)

    await release(client, seat, {"evidence": "performed", "session_nonce": NEW_NONCE},
                  key="new-key")
    watcher = await client.post("/memory/presence", json={
        "identity": seat, "project": PROJECT, "state": "running", "watcher": True,
    })
    assert watcher.status_code == 200, watcher.text
    assert watcher.json()["collision"] is None
    await assert_roster(client, seat, 0)
    assert await claim(client, "third-key", "third-process") != seat


@pytest.mark.asyncio
async def test_confirmed_release_preserves_live_peer_and_resumed_beat(client):
    seat = await claim(client, "old-key", OLD_NONCE)
    inferred_seat = await claim(client, "inferred-key", OLD_NONCE)
    await beat(client, inferred_seat, OLD_NONCE, 1)
    await beat(client, inferred_seat, NEW_NONCE, 2)
    await release(client, inferred_seat, {
        "evidence": "inferred", "session_nonce": OLD_NONCE,
    }, key="inferred-key")
    await assert_roster(client, inferred_seat, 2)
    await beat(client, inferred_seat, NEW_NONCE, 2)

    await beat(client, seat, OLD_NONCE, 1)
    await beat(client, seat, NEW_NONCE, 2)
    await release(client, seat, {"evidence": "observed", "session_nonce": OLD_NONCE})
    await assert_roster(client, seat, 1)
    await beat(client, seat, NEW_NONCE, 1)
    await beat(client, seat, OLD_NONCE, 2)


@pytest.mark.asyncio
async def test_released_nonce_can_send_the_first_heartbeat_after_release(client):
    seat = await claim(client, "old-key", OLD_NONCE)
    await beat(client, seat, OLD_NONCE, 1)
    await beat(client, seat, NEW_NONCE, 2)

    await release(client, seat, {"evidence": "observed", "session_nonce": OLD_NONCE})
    await assert_roster(client, seat, 1)
    await beat(client, seat, OLD_NONCE, 2)


@pytest.mark.parametrize("release_order", [None, "before", "after"],
                         ids=["takeover", "release-first", "cert-first"])
@pytest.mark.asyncio
async def test_death_and_successor_do_not_collide(client, release_order):
    seat = await claim(client, "old-key", OLD_NONCE)
    await beat(client, seat, OLD_NONCE, 1)
    fields = {"evidence": "performed", "session_nonce": OLD_NONCE}
    departures = []
    if release_order == "before":
        await release(client, seat, fields)
        departures.append(await roster_state(client, seat))
    response = await client.post("/session/death", json={
        "session_key": "old-key", "session_nonce": OLD_NONCE, "seat": seat,
        "project": PROJECT, "provider": "claude", "cause": "stop",
        "died_at": datetime.now(timezone.utc).isoformat(),
    })
    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "ok", "created": True, "updated": False,
        "cursor_updated": False, "cursor_size": 0,
    }
    departures.append(await roster_state(client, seat))
    if release_order == "after":
        await release(client, seat, fields)
        departures.append(await roster_state(client, seat))
    assert await claim(client, "new-key", NEW_NONCE) == seat
    before_beat = await roster_state(client, seat)
    await beat(client, seat, NEW_NONCE, 1)
    assert all(state == (0, False) for state in departures)
    assert before_beat == (0, False)


@pytest.mark.asyncio
async def test_presence_removal_rolls_back_with_release(client, db_pool, monkeypatch):
    from server.services import session_registry

    seat = await claim(client, "old-key", OLD_NONCE)
    await beat(client, seat, OLD_NONCE, 1)
    await beat(client, seat, NEW_NONCE, 2)
    fields = {"evidence": "performed", "session_nonce": OLD_NONCE}
    present_before_rollback = None

    @asynccontextmanager
    async def acquire():
        async with db_pool.acquire() as conn:
            @asynccontextmanager
            async def fail_before_commit():
                nonlocal present_before_rollback
                async with conn.transaction():
                    yield
                    present_before_rollback = await conn.fetchval(
                        """
                        SELECT metadata->'sessions' ? $1 FROM memories
                        WHERE scope = 'presence' AND user_id = $2 AND key = $3
                        """,
                        OLD_NONCE, PROJECT, f"presence/{seat}",
                    )
                    raise RuntimeError("simulated failure before release commit")

            yield SimpleNamespace(
                transaction=fail_before_commit, execute=conn.execute, fetchrow=conn.fetchrow,
            )

    async def get_pool():
        return SimpleNamespace(acquire=acquire)

    with monkeypatch.context() as patch:
        patch.setattr(session_registry, "get_pool", get_pool)
        response = await client.post("/session/release", json={
            "project": PROJECT, "session_key": "old-key", **fields,
        })
        assert response.status_code == 500, response.text
    assert present_before_rollback is False
    await assert_roster(client, seat, 2)
    await beat(client, seat, NEW_NONCE, 2)

    await release(client, seat, fields)
    await assert_roster(client, seat, 1)
    await beat(client, seat, NEW_NONCE, 1)
