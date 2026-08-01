"""MSG-5 / SEAT-7 — is anyone LISTENING at this address?

A session that never armed `engram-inbox-wait` is fully addressable and
permanently silent: mail is accepted, stored, and never wakes anybody. Nothing
reported that, so "nobody is listening" was indistinguishable from "not read
yet."

The watcher now beats. It is the right proxy precisely because it polls on its
own timer and lives exactly as long as the session, so it reports EXISTENCE
where the bridge heartbeat reports ACTIVITY.

The vocabulary is three-valued and matches the one AgentBeast uses for its
process-ancestry field, so the two sources never have to be reconciled:
true = an ear beat recently · false = one used to and stopped · null = no
watcher has ever beaten here, so there is no basis. null is never coerced to
false.
"""

import json as _json
from datetime import datetime, timedelta, timezone

import pytest as _pytest


async def _roster_entry(client, project, identity):
    r = await client.post("/memory/roster", json={"project": project})
    assert r.status_code == 200
    hits = [e for e in r.json()["entries"] if e["identity"] == identity]
    return hits[0] if hits else None


@_pytest.mark.asyncio
async def test_never_beaten_is_unknown_not_deaf(client, db_pool):
    """No watcher has ever reported → null. Absent is not dead.

    Coercing this to false is the original sin the seat work kept tripping
    over: it makes a live session look unreachable, and an unreachable
    session look reclaimable.
    """
    await client.post("/memory/presence", json={
        "identity": "earless", "project": "earless", "state": "running",
        "provider": "claude", "session_nonce": "n1",
    })
    entry = await _roster_entry(client, "earless", "earless")
    assert entry["watcher_alive"] is None
    assert entry["watcher_last_seen"] is None

    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE key = 'presence/earless'")


@_pytest.mark.asyncio
async def test_watcher_beat_reports_listening(client, db_pool):
    await client.post("/memory/presence", json={
        "identity": "eary", "project": "eary", "state": "running",
        "provider": "claude", "session_nonce": "n1",
    })
    r = await client.post("/memory/presence", json={
        "identity": "eary", "project": "eary", "state": "running",
        "watcher": True,
    })
    assert r.status_code == 200

    entry = await _roster_entry(client, "eary", "eary")
    assert entry["watcher_alive"] is True
    assert entry["watcher_last_seen"] is not None

    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE key = 'presence/eary'")


@_pytest.mark.asyncio
async def test_watcher_that_stopped_reads_deaf(client, db_pool):
    """Silence is evidence ONLY once there was a signal to lose.

    This is the state worth acting on: running and addressable, but no live
    ear. It is a "don't expect a reply" signal, never a reclaim signal.
    """
    await client.post("/memory/presence", json={
        "identity": "wentdeaf", "project": "wentdeaf", "state": "running",
        "provider": "claude", "session_nonce": "n1",
    })
    await client.post("/memory/presence", json={
        "identity": "wentdeaf", "project": "wentdeaf", "state": "running",
        "watcher": True,
    })
    # age the watcher beat past its window, leaving the session itself fresh
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE memories
            SET metadata = jsonb_set(metadata, '{watcher_last_seen}', to_jsonb($1::text), true)
            WHERE key = 'presence/wentdeaf' AND scope = 'presence'
            """,
            old,
        )

    entry = await _roster_entry(client, "wentdeaf", "wentdeaf")
    assert entry["watcher_alive"] is False
    assert entry["is_stale"] is False  # alive + deaf: the dangerous pair

    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE key = 'presence/wentdeaf'")


@_pytest.mark.asyncio
async def test_watcher_beat_never_flags_a_collision(client, db_pool):
    """The watcher shares its session's identity.

    If its beat joined the nonce map it would read as a second live session
    and false-flag the exact collision seats exist to detect — the detector
    firing on the fix. One watcher per session is correct, not a
    misconfiguration.
    """
    await client.post("/memory/presence", json={
        "identity": "solo", "project": "solo", "state": "running",
        "provider": "claude", "session_nonce": "only-one",
    })
    r = await client.post("/memory/presence", json={
        "identity": "solo", "project": "solo", "state": "running",
        "watcher": True,
    })
    assert r.json()["collision"] is None

    entry = await _roster_entry(client, "solo", "solo")
    assert entry["collision"] is False
    assert entry["live_sessions"] == 1
    assert entry["watcher_alive"] is True

    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE key = 'presence/solo'")


@_pytest.mark.asyncio
async def test_watcher_beat_does_not_revert_reported_state(client, db_pool):
    """A watcher beat carries no state, so it must not overwrite one.

    The session reports awaiting-input; the watcher keeps beating because it
    is alive. If the beat wrote a default the roster would silently flip the
    session back to 'running' — an observer would see a busy worker where one
    is actually blocked on input.
    """
    await client.post("/memory/presence", json={
        "identity": "blocked", "project": "blocked", "state": "awaiting-input",
        "provider": "grok", "channels": ["#devagents"], "session_nonce": "n1",
    })
    await client.post("/memory/presence", json={
        "identity": "blocked", "project": "blocked", "state": "running",
        "watcher": True,
    })

    entry = await _roster_entry(client, "blocked", "blocked")
    assert entry["provider"] == "grok"
    assert entry["channels"] == ["#devagents"]
    assert entry["watcher_alive"] is True
    # `state` left the roster payload on 2026-08-01 (one distinct value across
    # every row ever recorded), but the no-clobber behaviour it guarded is
    # real, so the assertion moves to the metadata where the field still lives.
    assert entry["state"] == "awaiting-input"  # shim serves the claim verbatim
    async with db_pool.acquire() as conn:
        md = await conn.fetchval(
            "SELECT metadata FROM memories WHERE key = 'presence/blocked'"
        )
        md = _json.loads(md) if isinstance(md, str) else md
        assert md["state"] == "awaiting-input"   # session's report survives
        await conn.execute("DELETE FROM memories WHERE key = 'presence/blocked'")


@_pytest.mark.asyncio
async def test_watcher_beat_does_not_invent_a_session(client, db_pool):
    """No presence row → no session has ever heartbeated here.

    Creating one from a watcher beat would conjure a session that does not
    exist and put it on the roster as running.
    """
    r = await client.post("/memory/presence", json={
        "identity": "ghost", "project": "ghost", "state": "running",
        "watcher": True,
    })
    assert r.status_code == 200  # best-effort, never an error to the caller

    entry = await _roster_entry(client, "ghost", "ghost")
    assert entry is None


@_pytest.mark.asyncio
async def test_watcher_beat_refreshes_the_seat(client, db_pool):
    """SEAT-7: the fix for 'quiet is not dead'.

    Seat liveness tracked tool activity, so a session working uninterrupted
    aged past the live window and its address became reclaimable while it was
    genuinely alive and listening. No time threshold can distinguish quiet
    from dead — a bigger number is not a different kind of answer. The watcher
    can, because it beats whether or not the session is doing anything.
    """
    claim = await client.post("/session/claim", json={
        "session_key": "watcher-seat-test", "project": "seaty",
        "provider": "claude", "session_nonce": "n1",
    })
    assert claim.status_code == 200
    seat = claim.json()["seat"]

    await client.post("/memory/presence", json={
        "identity": seat, "project": "seaty", "state": "running",
        "provider": "claude", "session_nonce": "n1",
    })

    # Age BOTH rows, as an uninterrupted session would.
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE memories SET last_used_at = NOW() - INTERVAL '3 hours' "
            "WHERE key IN ($1, $2)", f"presence/{seat}", f"seat/{seat}",
        )

    await client.post("/memory/presence", json={
        "identity": seat, "project": "seaty", "state": "running",
        "watcher": True,
    })

    async with db_pool.acquire() as conn:
        age = await conn.fetchval(
            "SELECT EXTRACT(EPOCH FROM (NOW() - last_used_at)) FROM memories "
            "WHERE key = $1 AND scope = 'seat'", f"seat/{seat}",
        )
    assert age < 60, "watcher beat must refresh the seat, or a live session's address can be taken"

    await client.post("/session/release", json={"session_key": "watcher-seat-test"})
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE key IN ($1, $2)",
            f"presence/{seat}", f"seat/{seat}",
        )


# --- The goodbye (2026-08-01) -------------------------------------------
#
# A watcher OBSERVES its session's exit rather than announcing its own. These
# three tests exist because the property that makes that safe is a discipline,
# not a mechanism, and a discipline with no test is a paragraph.


@_pytest.mark.asyncio
async def test_a_missing_goodbye_changes_nothing(client, db_pool):
    """ABSENCE OF A FAREWELL IS EVIDENCE OF NOTHING.

    The failure this guards is not someone disagreeing with the rule. It is a
    reader six months out finding "we have goodbyes now" and reaching for a
    MISSING one as a signal — reasoning correctly from a false premise. A
    watcher killed on its own sends nothing; so does a machine that lost power.
    Inferring from silence rebuilds the 600-second window under a better name.

    So: a row that never said goodbye must be byte-identical to one from
    before the feature existed.
    """
    ident, proj = "nogoodbye", "nogoodbyeproj"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='presence' AND user_id=$1", proj)

    await client.post("/memory/presence", json={
        "identity": ident, "project": proj, "state": "running"})
    await client.post("/memory/presence", json={
        "identity": ident, "project": proj, "state": "running", "watcher": True})

    entry = await _roster_entry(client, proj, ident)
    assert entry["farewell_at"] is None, "a farewell was invented from silence"
    assert entry["watcher_alive"] is True
    assert entry["is_stale"] is False

    # And the send path must not warn on a healthy, silent-about-death seat.
    r = await client.post("/memory/send", json={
        "to": ident, "from_": "tester", "subject": "s", "body": "b",
        "intent": "action"})
    assert r.json().get("recipient_warnings") is None, (
        "absence of a goodbye produced a warning — silence became a signal"
    )

    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='presence' AND user_id=$1", proj)
        await conn.execute("DELETE FROM memories WHERE scope='inbox' AND user_id=$1", ident)


@_pytest.mark.asyncio
async def test_a_farewell_is_voided_by_any_later_life(client, db_pool):
    """REVOCATION — the rule that makes a wrong farewell survivable.

    A farewell can be wrong. The session it libels cannot correct the record by
    speaking up, because being unheard is the premise of the mistake — so
    without revocation a false farewell is silent AND permanent, and the
    session merely loses its address later rather than immediately.

    Any evidence of life voids it. That converts the failure from permanent to
    transient: the moment the session or a re-armed watcher speaks, the record
    heals itself.
    """
    ident, proj = "revoked", "revokedproj"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='presence' AND user_id=$1", proj)

    await client.post("/memory/presence", json={
        "identity": ident, "project": proj, "state": "running"})
    await client.post("/memory/presence", json={
        "identity": ident, "project": proj, "state": "running", "farewell": True})
    entry = await _roster_entry(client, proj, ident)
    assert entry["farewell_at"] is not None, "the observed exit was not recorded"

    # 1. The session itself speaks — the strongest possible evidence of life.
    await client.post("/memory/presence", json={
        "identity": ident, "project": proj, "state": "running"})
    entry = await _roster_entry(client, proj, ident)
    assert entry["farewell_at"] is None, (
        "a heartbeat did not void the farewell — the session cannot clear its "
        "own death notice, so a wrong one would be permanent"
    )

    # 2. A re-armed watcher speaks, on a row that was declared dead again.
    await client.post("/memory/presence", json={
        "identity": ident, "project": proj, "state": "running", "farewell": True})
    assert (await _roster_entry(client, proj, ident))["farewell_at"] is not None
    await client.post("/memory/presence", json={
        "identity": ident, "project": proj, "state": "running", "watcher": True})
    entry = await _roster_entry(client, proj, ident)
    assert entry["farewell_at"] is None, (
        "a watcher beat did not void the farewell — a re-armed ear is life"
    )

    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='presence' AND user_id=$1", proj)


@_pytest.mark.asyncio
async def test_a_farewell_never_invents_a_session(client, db_pool):
    """No presence row → nothing to say goodbye about.

    Same discipline as the watcher beat refusing to INSERT, and sharper here:
    conjuring a row from a farewell would invent a session for the sole purpose
    of declaring it dead.
    """
    r = await client.post("/memory/presence", json={
        "identity": "neverwas", "project": "neverwasproj", "state": "running",
        "farewell": True})
    assert r.status_code == 200  # best-effort, never an error to the caller
    async with db_pool.acquire() as conn:
        row = await conn.fetchval(
            "SELECT count(*) FROM memories WHERE key = 'presence/neverwas'")
    assert row == 0, "a farewell conjured a session in order to bury it"
