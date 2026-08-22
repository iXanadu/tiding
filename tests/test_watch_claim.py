"""Watch-claim step 1: nonce-CAS claim + beat (design v2, reviewed).

Every test here guards a specific review finding or wild specimen from
2026-08-20. The dual-holder test is the reviewer's S2 race verbatim; the
partial-refusal test is F10's wild specimen (a watcher holding coverage for
a seat it was not listening on, while every liveness probe passed).
"""

import asyncio
import uuid

import pytest

from server.services.watch_claim import (
    WATCH_EXPIRY_SECONDS,
    mint_nonce,
    watch_beat,
    watch_claim,
    watch_release,
    watch_status,
)


def _seat():
    return f"wcx-{uuid.uuid4().hex[:8]}-claude-2"


def _ls(seat):
    # a realistic listen set: seat, lane, project channel
    lane = seat.rsplit("-", 1)[0]
    proj = lane.rsplit("-", 1)[0]
    return [seat, lane, proj]


@pytest.mark.asyncio
async def test_claim_grant_and_second_arrival_held(db_pool):
    seat = _seat()
    n1, n2 = mint_nonce(), mint_nonce()

    r1 = await watch_claim(seat, n1, "bridge", "/tmp/p", _ls(seat))
    assert r1["verdict"] == "granted"

    r2 = await watch_claim(seat, n2, "ab", "/tmp/p", _ls(seat))
    assert r2["verdict"] == "held"
    # K1: the loser is TOLD to retry on a timer — the contract rides the
    # response, not prose. v1's exit-forever meant mail died with the holder.
    assert r2["retry_after_seconds"] > 0
    assert r2["holder_armed_by"] == "bridge"


@pytest.mark.asyncio
async def test_partial_listen_set_is_refused_not_granted(db_pool):
    """F10, the wild specimen: AB's bare watcher held 'coverage' for
    agentbeast-app-grok-2 while listening only on channel+lane. A
    seat-addressed DM could never wake it; every liveness probe passed."""
    seat = _seat()
    lane = seat.rsplit("-", 1)[0]
    proj = lane.rsplit("-", 1)[0]

    r = await watch_claim(seat, mint_nonce(), "ab", "/tmp/p",
                          [lane, proj])           # channel+lane, NO seat
    assert r["verdict"] == "partial-refused"
    assert "partial watch is not a watch" in r["reason"]

    # and nothing was written — the register must not show it covered
    assert (await watch_status(seat))["state"] == "unheld"


@pytest.mark.asyncio
async def test_beat_is_cas_stalled_holder_cannot_refresh_a_stolen_row(db_pool):
    """The reviewer's S2 dual-holder race, verbatim:
      1. A holds; its beat stalls past EXPIRY.
      2. B steals via the expiry branch.
      3. A's in-flight beat lands late.
    v1's naive `UPDATE last_beat WHERE seat=` would refresh the row B just
    took, leaving BOTH believing they hold — double delivery. The CAS makes
    A's late beat match nothing and come back `displaced`."""
    seat = _seat()
    na, nb = mint_nonce(), mint_nonce()

    assert (await watch_claim(seat, na, "bridge", "/tmp/p", _ls(seat)))["verdict"] == "granted"

    # age A's beat past expiry (simulate the stall by rewriting last_beat)
    async with db_pool.acquire() as conn:
        await conn.execute(
            """UPDATE memories
               SET metadata = jsonb_set(metadata, '{last_beat}',
                   to_jsonb((NOW() - make_interval(secs => $2))::text), false)
               WHERE key = $1""",
            f"watch/{seat}", WATCH_EXPIRY_SECONDS + 30,
        )

    rb = await watch_claim(seat, nb, "bridge", "/tmp/p", _ls(seat))
    assert rb["verdict"] == "granted" and rb.get("stolen"), "expiry steal failed"

    # A's late beat arrives — it must NOT refresh B's row
    ra = await watch_beat(seat, na)
    assert ra["verdict"] == "displaced", (
        "the stalled holder refreshed a stolen row — dual holder, the exact "
        "race the review found in v1"
    )
    # and B still beats fine
    assert (await watch_beat(seat, nb))["verdict"] == "holder"


@pytest.mark.asyncio
async def test_live_holder_cannot_be_stolen(db_pool):
    seat = _seat()
    na = mint_nonce()
    await watch_claim(seat, na, "bridge", "/tmp/p", _ls(seat))
    await watch_beat(seat, na)  # fresh

    rb = await watch_claim(seat, mint_nonce(), "ab", "/tmp/p", _ls(seat))
    assert rb["verdict"] == "held", "a beating holder was stolen from"


@pytest.mark.asyncio
async def test_same_instant_race_has_exactly_one_winner(db_pool):
    """The unique-key branch: N concurrent first claims, one grant."""
    seat = _seat()
    results = await asyncio.gather(*[
        watch_claim(seat, mint_nonce(), "bridge", "/tmp/p", _ls(seat))
        for _ in range(6)
    ])
    verdicts = [r["verdict"] for r in results]
    assert verdicts.count("granted") == 1, verdicts
    assert verdicts.count("held") == 5, verdicts


@pytest.mark.asyncio
async def test_release_is_cas_too(db_pool):
    """Releasing a watch someone else now holds would free THEIR claim —
    the same ghost class, on the way out."""
    seat = _seat()
    na, nb = mint_nonce(), mint_nonce()
    await watch_claim(seat, na, "bridge", "/tmp/p", _ls(seat))

    # steal after expiry
    async with db_pool.acquire() as conn:
        await conn.execute(
            """UPDATE memories SET metadata = jsonb_set(metadata, '{last_beat}',
               to_jsonb((NOW() - make_interval(secs => $2))::text), false)
               WHERE key = $1""",
            f"watch/{seat}", WATCH_EXPIRY_SECONDS + 30)
    await watch_claim(seat, nb, "bridge", "/tmp/p", _ls(seat))

    ra = await watch_release(seat, na)
    assert ra["verdict"] == "not-holder", "a displaced process released the successor's claim"
    assert (await watch_status(seat))["state"] == "covered"

    rb = await watch_release(seat, nb)
    assert rb["verdict"] == "released"
    assert (await watch_status(seat))["state"] == "unheld"


@pytest.mark.asyncio
async def test_status_is_three_valued_and_unheld_is_not_dead(db_pool):
    """K3/I6: unheld is a state, not a verdict. A session may be running
    UNHELD legitimately (store unreachable at arm time); the register just
    never shows it as covered."""
    seat = _seat()
    assert (await watch_status(seat))["state"] == "unheld"

    n = mint_nonce()
    await watch_claim(seat, n, "agent", "/tmp/p", _ls(seat))
    assert (await watch_status(seat))["state"] == "covered"

    async with db_pool.acquire() as conn:
        await conn.execute(
            """UPDATE memories SET metadata = jsonb_set(metadata, '{last_beat}',
               to_jsonb((NOW() - make_interval(secs => $2))::text), false)
               WHERE key = $1""",
            f"watch/{seat}", WATCH_EXPIRY_SECONDS + 30)
    st = await watch_status(seat)
    assert st["state"] == "expired"
    assert st["armed_by"] == "agent"


# ─── endpoint layer ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_endpoints_roundtrip(client, db_pool):
    seat = _seat()
    n = mint_nonce()
    r = await client.post("/session/watch/claim", json={
        "seat": seat, "nonce": n, "armed_by": "bridge",
        "project_dir": "/tmp/p", "listen_set": _ls(seat),
    })
    assert r.status_code == 200 and r.json()["verdict"] == "granted"

    r2 = await client.post("/session/watch/beat", json={"seat": seat, "nonce": n})
    assert r2.json()["verdict"] == "holder"

    r3 = await client.get(f"/session/watch/status?seat={seat}")
    assert r3.json()["state"] == "covered"

    r4 = await client.post("/session/watch/release", json={"seat": seat, "nonce": n})
    assert r4.json()["verdict"] == "released"
    assert (await client.get(f"/session/watch/status?seat={seat}")).json()["state"] == "unheld"


# ─── step 5: delivery-liveness displacement (K2, the monopoly kill) ─────────

async def _send_mail(client, to):
    r = await client.post("/memory/send", json={
        "to": to, "subject": "k2 probe", "body": "x", "intent": "action"})
    assert r.status_code == 200
    return r.json()["id"]


@pytest.mark.asyncio
async def test_a_beating_but_mute_holder_is_displaceable(client, db_pool):
    """The wild specimen, as a protocol rule: a holder that BEATS but never
    FETCHES looked exactly like coverage (huddle-fast, DM-deaf), and a naive
    exclusive claim would have LOCKED OUT the working watcher behind it."""
    from server.services.watch_claim import MUTE_GRACE_SECONDS
    seat = _seat()
    na, nb = mint_nonce(), mint_nonce()
    assert (await watch_claim(seat, na, "ab", "/tmp/p", _ls(seat)))["verdict"] == "granted"
    await watch_beat(seat, na)   # alive and beating…

    mid = await _send_mail(client, seat)   # …mail arrives…
    # …and outwaits the mute grace with fetched_through never advancing
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE memories SET created_at = NOW() - make_interval(secs => $2) "
            "WHERE key = $1", mid, MUTE_GRACE_SECONDS + 60)
        # holder claimed before the mail did (so the mail postdates claimed_at)
        await conn.execute(
            """UPDATE memories SET metadata = jsonb_set(metadata, '{claimed_at}',
               to_jsonb((NOW() - make_interval(secs => $2))::text), false)
               WHERE key = $1""",
            f"watch/{seat}", MUTE_GRACE_SECONDS + 3600)

    rb = await watch_claim(seat, nb, "bridge", "/tmp/p", _ls(seat))
    assert rb["verdict"] == "granted" and rb.get("stolen"), (
        "a beating-but-mute holder kept its monopoly — K2 regression: the "
        "working watcher stays locked out behind a healthy-looking corpse"
    )
    assert (await watch_beat(seat, na))["verdict"] == "displaced"


@pytest.mark.asyncio
async def test_a_holder_that_fetches_is_not_displaceable(client, db_pool):
    """The other edge: advancing fetched_through past the waiting mail is
    proof of delivery — a working holder must never be stolen from."""
    from datetime import datetime, timedelta, timezone
    from server.services.watch_claim import MUTE_GRACE_SECONDS
    seat = _seat()
    na = mint_nonce()
    await watch_claim(seat, na, "bridge", "/tmp/p", _ls(seat))

    mid = await _send_mail(client, seat)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE memories SET created_at = NOW() - make_interval(secs => $2) "
            "WHERE key = $1", mid, MUTE_GRACE_SECONDS + 60)

    # holder reports it has fetched THROUGH now — delivery is proven
    now_iso = datetime.now(timezone.utc).isoformat()
    assert (await watch_beat(seat, na, fetched_through=now_iso))["verdict"] == "holder"

    rb = await watch_claim(seat, mint_nonce(), "bridge", "/tmp/p", _ls(seat))
    assert rb["verdict"] == "held", (
        "a DELIVERING holder was stolen from — fetched_through is not being "
        "honored, so every busy watcher is now displaceable"
    )


@pytest.mark.asyncio
async def test_held_retry_after_tracks_holder_expiry_not_flat(db_pool):
    """WATCH-CLAIM-4(a), wild specimen 2026-08-21 14:01Z: a successor that
    claims seconds BEFORE the predecessor's beat ages out was told to retry
    in a flat EXPIRY (150s) and sat uncovered 3.5 minutes over a claim that
    was stealable 8s later. `retry_after_seconds` must be the time until the
    holder is stealable — floored, and never above EXPIRY."""
    seat = _seat()
    na, nb = mint_nonce(), mint_nonce()
    assert (await watch_claim(seat, na, "bridge", "/tmp/p", _ls(seat)))["verdict"] == "granted"

    # fresh holder: retry is (close to) the full window, never above it
    r_fresh = await watch_claim(seat, nb, "bridge", "/tmp/p", _ls(seat))
    assert r_fresh["verdict"] == "held"
    assert WATCH_EXPIRY_SECONDS - 10 <= r_fresh["retry_after_seconds"] <= WATCH_EXPIRY_SECONDS

    # age the holder's beat to 10s short of expiry: retry must be ~10s, not 150
    async with db_pool.acquire() as conn:
        await conn.execute(
            """UPDATE memories
               SET metadata = jsonb_set(metadata, '{last_beat}',
                   to_jsonb((NOW() - make_interval(secs => $2))::text), false)
               WHERE key = $1""",
            f"watch/{seat}", WATCH_EXPIRY_SECONDS - 10,
        )
    r_late = await watch_claim(seat, nb, "bridge", "/tmp/p", _ls(seat))
    assert r_late["verdict"] == "held"
    assert 5 <= r_late["retry_after_seconds"] <= 20, r_late

    # and once stealable, the same claim is GRANTED (the existing steal path)
    async with db_pool.acquire() as conn:
        await conn.execute(
            """UPDATE memories
               SET metadata = jsonb_set(metadata, '{last_beat}',
                   to_jsonb((NOW() - make_interval(secs => $2))::text), false)
               WHERE key = $1""",
            f"watch/{seat}", WATCH_EXPIRY_SECONDS + 5,
        )
    r_steal = await watch_claim(seat, nb, "bridge", "/tmp/p", _ls(seat))
    assert r_steal["verdict"] == "granted" and r_steal.get("stolen")



@pytest.mark.asyncio
async def test_claim_follows_the_seat_occupants_watcher_displaces_a_corpse_at_once(db_pool):
    """WATCH-CLAIM-4(b): a watch is keyed by seat NAME; the seat REGISTER is
    the authority on who occupies the name (per-process session_nonce,
    refreshed on every re-claim). The occupant's watcher — the one carrying
    the register's CURRENT nonce — displaces an incumbent watch that does not
    carry it immediately, beats or no beats. A claimant the register does not
    recognize as the occupant still waits out the old rules."""
    from server.services.session_registry import seat_claim
    proj = f"wcfs{uuid.uuid4().hex[:8]}"
    key = f"wcfs-key-{uuid.uuid4().hex[:8]}"
    # Incarnation 1 of a session: seat row nonce n1; its watcher claims with it.
    r1 = await seat_claim(session_key=key, project=proj, provider="claude",
                          session_nonce="n1-" + uuid.uuid4().hex[:6], host="h")
    seat = r1["seat"]
    n1 = r1.get("session_nonce") or None
    # The register's row is what matters, read it back rather than trust r1's shape.
    async with db_pool.acquire() as conn:
        n1 = await conn.fetchval(
            "SELECT metadata->>'session_nonce' FROM memories WHERE scope='seat' AND key=$1",
            f"seat/{seat}")
    assert n1
    wa = mint_nonce()
    ra = await watch_claim(seat, wa, "bridge", "/tmp/p", _ls(seat), seat_nonce=n1)
    assert ra["verdict"] == "granted"
    await watch_beat(seat, wa)  # the corpse keeps beating (hard-killed bridge, live watcher)
    try:
        # A stranger (no nonce / wrong nonce) is still held by a beating holder.
        rs = await watch_claim(seat, mint_nonce(), "ab", "/tmp/p", _ls(seat))
        assert rs["verdict"] == "held"
        rs = await watch_claim(seat, mint_nonce(), "bridge", "/tmp/p", _ls(seat),
                               seat_nonce="not-the-occupant")
        assert rs["verdict"] == "held"
        # Incarnation 2: same session restarted (new process nonce) re-claims the
        # seat — SEAT-9 newest-wins rewrites the register's nonce.
        n2 = "n2-" + uuid.uuid4().hex[:6]
        r2 = await seat_claim(session_key=key, project=proj, provider="claude",
                              session_nonce=n2, host="h")
        assert r2["seat"] == seat
        async with db_pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT metadata->>'session_nonce' FROM memories WHERE scope='seat' AND key=$1",
                f"seat/{seat}") == n2
        # Its watcher claims with the register's CURRENT nonce: granted NOW,
        # over a beating incumbent, with the reason named.
        wb = mint_nonce()
        rb = await watch_claim(seat, wb, "bridge", "/tmp/p", _ls(seat), seat_nonce=n2)
        assert rb["verdict"] == "granted", rb
        assert rb.get("stolen") is True
        assert rb.get("displaced_reason") == "claim-follows-seat"
        # The corpse's next beat reads displaced; the successor holds.
        assert (await watch_beat(seat, wa))["verdict"] == "displaced"
        assert (await watch_beat(seat, wb))["verdict"] == "holder"
        # And the occupant's own live watcher is NOT displaced by a second
        # claimant carrying the same (correct) nonce — that one is held.
        rc = await watch_claim(seat, mint_nonce(), "bridge", "/tmp/p", _ls(seat), seat_nonce=n2)
        assert rc["verdict"] == "held"
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM memories WHERE (scope IN ('seat','presence','death','watch','lane-cursor') "
                "AND (project = $1 OR key = $2 OR user_id = $1))",
                proj, f"watch/{seat}")
