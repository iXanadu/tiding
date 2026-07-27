"""Seat registry (SEAT-3): sessions CLAIM addresses, they don't compute them.

The invariant under test is the one docs/messaging.md always asserted but
nothing enforced: two agents never share an identity. Before this, three Claude
sessions in one folder all computed the seat "<project>-claude" and silently
shared ack-state, unable to wake each other.
"""

import asyncio

import pytest

from server.services.memory_service import INBOX_NAMESPACE
from server.services.session_registry import (
    SEAT_GRACE_SECONDS,
    SEAT_LIVE_SECONDS,
)

PROJ = "seattest"


async def _clear(db_pool, project=PROJ):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope = 'seat' AND project = $1", project
        )
        await conn.execute(
            "DELETE FROM memories WHERE scope = 'inbox' AND user_id LIKE $1",
            f"{project}%",
        )


async def _age_seat(db_pool, seat, seconds, project=PROJ):
    """Backdate a seat's heartbeat so liveness/grace windows can be exercised."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE memories SET last_used_at = NOW() - ($1 || ' seconds')::interval
            WHERE scope = 'seat' AND project = $2 AND key = $3
            """,
            str(seconds), project, f"seat/{seat}",
        )


def _claim(client, key, **kw):
    body = {"session_key": key, "project": PROJ, "provider": "claude"}
    body.update(kw)
    return client.post("/session/claim", json=body)


@pytest.mark.asyncio
async def test_three_same_provider_sessions_get_three_addresses(client, db_pool):
    """Rob's case: orchestrator + tester + implementer, all Claude, one folder.

    Every one of them asks for the SAME preferred seat, exactly as a launcher
    computing "<project>-<provider>" would. They must not collide.
    """
    await _clear(db_pool)
    seats = []
    for key in ("orchestrator", "tester", "implementer"):
        r = await _claim(client, key, preferred_seat=f"{PROJ}-claude")
        assert r.status_code == 200
        seats.append(r.json()["seat"])

    assert seats == [f"{PROJ}-claude", f"{PROJ}-claude-2", f"{PROJ}-claude-3"]
    assert len(set(seats)) == 3
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_concurrent_claims_never_collide(client, db_pool):
    """The allocator is a compare-and-swap on the existing UNIQUE constraint.

    No advisory locks: concurrent inserts on one candidate are serialised by
    the index, the loser advances to the next ordinal.
    """
    await _clear(db_pool)
    results = await asyncio.gather(
        *(_claim(client, f"racer-{i}") for i in range(10))
    )
    seats = [r.json()["seat"] for r in results]
    assert len(set(seats)) == 10, f"duplicate seat handed out: {seats}"
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_reclaim_is_idempotent_so_a_restart_keeps_its_address(client, db_pool):
    """A bridge restart must not burn an ordinal or move a live session."""
    await _clear(db_pool)
    first = (await _claim(client, "steady")).json()
    again = (await _claim(client, "steady")).json()
    assert again["seat"] == first["seat"]
    assert again["is_new"] is False
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_a_respawn_takes_its_address_back_from_a_live_looking_holder(
    client, db_pool
):
    """NEWEST-WINS (SEAT-9, 2026-07-26). A DELIBERATE POLICY REVERSAL.

    This test previously asserted the opposite: a new nonce on a live-looking
    holder was separated onto its own ordinal and warned about, on the grounds
    that two processes sharing one key is the collision seats exist to prevent.

    That guard could not tell a rival session from a RESTART, because seconds
    after a process dies its seat row, presence row and watcher all still read
    fresh. So it fired on every respawn — the common case — and a launcher
    that injected a stable key and an explicit identity still got an address
    neither it nor its own watcher expected. On 2026-07-26 that sent a huddle
    invitation to two dead mailboxes: delivered, waking nobody.

    The class it defended against no longer arises: session_key derives from
    the tmux slot (or ppid + parent start time), and two LIVE workers cannot
    share a slot. The one case that does produce overlap — a launcher killing
    and relaunching with no wait — is a predecessor mid-teardown, which the
    one-way door below handles.
    """
    await _clear(db_pool)
    a = (await _claim(client, "shared-key", session_nonce="nonceA")).json()
    b = (await _claim(client, "shared-key", session_nonce="nonceB")).json()
    assert b["seat"] == a["seat"], "a respawn must get its own address back"
    assert b["is_new"] is False
    assert not b["warning"], "an ordinary respawn is not an alarm"
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_a_displaced_process_can_never_reclaim_its_seat(client, db_pool):
    """THE ONE-WAY DOOR — what makes newest-wins safe instead of a race.

    AgentBeast's grok path kills a tmux session and starts its replacement
    with ZERO wait, so the predecessor may still be exiting while the
    successor claims. Under naive newest-wins the dying process's final
    heartbeat would take the address back off the successor that had just
    been given it — a failure that strikes at random, which is worse than the
    predictable one it replaces.

    So displacement is permanent: the seat remembers who it was taken from.
    """
    await _clear(db_pool)
    old = (await _claim(client, "handover", session_nonce="dying")).json()
    new = (await _claim(client, "handover", session_nonce="successor")).json()
    assert new["seat"] == old["seat"]

    # The predecessor's last gasp, arriving after it already lost the seat.
    ghost = (await _claim(client, "handover", session_nonce="dying")).json()
    assert ghost["seat"] != old["seat"], (
        "a dying predecessor took the address back from its own successor"
    )
    assert ghost["warning"] and "displaced" in ghost["warning"]

    # And the successor still holds it, repeatedly.
    for _ in range(3):
        still = (await _claim(client, "handover", session_nonce="successor")).json()
        assert still["seat"] == old["seat"]
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_the_door_survives_several_restarts(client, db_pool):
    """The bound on remembered nonces must not let an old ghost back in.

    A long-lived seat accumulates displaced nonces across restarts. Within the
    bound, every one of them stays locked out — checked at the far end of a
    chain rather than only for the most recent predecessor.
    """
    await _clear(db_pool)
    first = (await _claim(client, "chain", session_nonce="gen0")).json()
    for gen in range(1, 5):
        nxt = (await _claim(client, "chain", session_nonce=f"gen{gen}")).json()
        assert nxt["seat"] == first["seat"]

    for gen in range(0, 4):
        ghost = (await _claim(client, "chain", session_nonce=f"gen{gen}")).json()
        assert ghost["seat"] != first["seat"], f"gen{gen} reclaimed the seat"
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_same_key_new_nonce_after_death_is_a_restart_not_a_collision(
    client, db_pool
):
    """The mirror of the test above: once the holder is no longer live, the
    same key with a new process nonce is a genuine restart and keeps its seat.
    Distinguishing them by holder liveness is what lets R3 hold without
    reopening the duplicate-key hole."""
    await _clear(db_pool)
    a = (await _claim(client, "restarter", session_nonce="old")).json()
    await _age_seat(db_pool, a["seat"], SEAT_LIVE_SECONDS + 60)
    b = (await _claim(client, "restarter", session_nonce="new")).json()
    assert b["seat"] == a["seat"]
    assert not b["warning"]
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_admin_is_never_allocated_apart(client, db_pool):
    """Deliberate role-sharing is a feature. Maintenance sessions across boxes
    wear one 'admin' identity on purpose; the registry must not 'fix' it."""
    r1 = await client.post("/session/claim", json={
        "session_key": "adm-1", "project": "admin", "provider": "claude",
        "preferred_seat": "admin",
    })
    r2 = await client.post("/session/claim", json={
        "session_key": "adm-2", "project": "admin", "provider": "claude",
        "preferred_seat": "admin",
    })
    assert r1.json()["seat"] == "admin"
    assert r2.json()["seat"] == "admin"


@pytest.mark.asyncio
async def test_abandoned_seat_is_reclaimed_lowest_first(client, db_pool):
    """Past the grace window a seat returns to the pool, and allocation is
    low-water-mark so numbering stays tight instead of drifting upward."""
    await _clear(db_pool)
    a = (await _claim(client, "ghost")).json()
    await _age_seat(db_pool, a["seat"], SEAT_GRACE_SECONDS + 60)
    b = (await _claim(client, "successor", host="otherbox")).json()
    assert b["seat"] == a["seat"]
    assert b["reclaimed_from"] == "ghost"
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_never_reclaim_a_seat_holding_undelivered_mail(client, db_pool):
    """R8 outranks R7: mail in flight for a dead session is preserved for its
    successor, never handed to a stranger who drew the same ordinal.

    Untidy numbering is a cosmetic cost; delivering someone else's mail is a
    correctness failure.
    """
    await _clear(db_pool)
    a = (await _claim(client, "ghost-with-mail")).json()
    seat = a["seat"]
    send = await client.post("/memory/send", json={
        "to": seat, "subject": "unread", "body": "for the dead session",
    })
    assert send.status_code == 200
    await _age_seat(db_pool, seat, SEAT_GRACE_SECONDS + 600)

    b = (await _claim(client, "successor-2", host="otherbox")).json()
    assert b["seat"] != seat, "reclaimed a seat that still held undelivered mail"
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_release_frees_the_ordinal_immediately(client, db_pool):
    """The clean path — better than waiting out the grace period."""
    await _clear(db_pool)
    a = (await _claim(client, "tidy")).json()
    rel = await client.post("/session/release", json={
        "session_key": "tidy", "project": PROJ,
    })
    assert rel.json()["released"] == a["seat"]
    b = (await _claim(client, "newcomer")).json()
    assert b["seat"] == a["seat"]  # lowest free ordinal, reused at once
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_no_role_alias_endpoint(client, db_pool):
    """A role is NOT an address (Rob, 2026-07-24). It is not unique or
    provider-stable — "engram-tester" for grok and for claude would collide,
    the exact bug seats kill. Roles are assigned in the huddle to whichever
    seats the owner picked; the addressing layer never carries them. The
    endpoint that briefly bound "<project>-<role>" as an address is gone."""
    r = await client.post("/session/alias", json={
        "session_key": "lead", "project": PROJ, "alias": "orchestrator",
    })
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_seat_rows_are_not_writable_through_the_generic_memory_path(client):
    """Registry rows have their own lifecycle. If /memory/set could reach them a
    writer could hand itself an address the registry believes someone else
    holds — the collision, laundered through the memory API."""
    r = await client.post("/memory/set", json={
        "namespace": "fleet", "key": "seat/hijack", "value": "mine",
        "scope": "seat", "user_id": "global",
    })
    assert r.status_code == 400
    assert "own endpoints" in r.json()["detail"]


@pytest.mark.asyncio
async def test_seats_endpoint_reports_liveness(client, db_pool):
    await _clear(db_pool)
    a = (await _claim(client, "visible")).json()
    r = await client.post("/session/seats", json={"project": PROJ})
    entry = [s for s in r.json()["seats"] if s["seat"] == a["seat"]][0]
    assert entry["is_live"] is True
    assert entry["reclaimable"] is False
    assert entry["session_key"] == "visible"
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_launcher_can_read_back_the_granted_seat(client, db_pool):
    """A launcher never calls /session/claim — the bridge inside the session
    does — so the granted seat must be readable by the one join a launcher
    owns: the session key it generated.

    Without this a launcher reconstructs the seat locally and misses SILENTLY
    whenever an ordinal was granted (AgentBeast's provider-badge map keys on
    exactly such a reconstruction, and an unbadged row reads as a broken
    client). Requested by agentbeast 2026-07-24, inside the deploy window.
    """
    await _clear(db_pool)
    # Two sessions want the same preferred seat; the second is granted -2.
    await _claim(client, "launcher-key-a", preferred_seat=f"{PROJ}-claude")
    second = (await _claim(
        client, "launcher-key-b", preferred_seat=f"{PROJ}-claude"
    )).json()
    assert second["seat"] == f"{PROJ}-claude-2"

    r = await client.post("/session/seats", json={"session_key": "launcher-key-b"})
    entries = r.json()["seats"]
    assert len(entries) == 1, "session_key must be a direct lookup, not a scan"
    assert entries[0]["seat"] == f"{PROJ}-claude-2"
    assert entries[0]["session_key"] == "launcher-key-b"
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_readback_carries_provider_so_a_launcher_need_not_infer_it(
    client, db_pool
):
    """The readback is what lets a badge map key on the granted seat AND read
    the provider as a field, instead of parsing either off the address string —
    a "-2" tail is an ordinal, a "-grok" tail is a provider, and nothing in the
    string reliably tells them apart."""
    await _clear(db_pool)
    await _claim(client, "badged", provider="grok")
    r = await client.post("/session/seats", json={"session_key": "badged"})
    entry = r.json()["seats"][0]
    assert entry["provider"] == "grok"
    assert entry["seat"].startswith(f"{PROJ}-grok")
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_heartbeat_refreshes_the_seat_so_a_live_session_stays_live(
    client, db_pool
):
    """SEAT-8: the presence heartbeat must keep the seat row fresh.

    The seat and the presence row are two clocks on one session. The seat's
    was written once at claim time and never refreshed, so they disagreed in
    production: the roster reported a session fresh at 374s while its seat
    read is_live=false, reclaimable=true — at which point a newcomer could be
    granted an address a running session still held.
    """
    await _clear(db_pool)
    a = (await _claim(client, "beating")).json()
    # Age the seat past the grace window, as a long-running session's would.
    await _age_seat(db_pool, a["seat"], SEAT_GRACE_SECONDS + 600)

    listed = (await client.post("/session/seats",
                                json={"session_key": "beating"})).json()["seats"][0]
    assert listed["is_live"] is False  # stale, pre-heartbeat

    # One heartbeat at that identity — exactly what a live session sends.
    beat = await client.post("/memory/presence", json={
        "identity": a["seat"], "project": PROJ, "state": "running",
        "provider": "claude", "session_nonce": "n1",
    })
    assert beat.status_code == 200

    after = (await client.post("/session/seats",
                               json={"session_key": "beating"})).json()["seats"][0]
    assert after["is_live"] is True, "a heartbeating session must not read as dead"
    assert after["reclaimable"] is False, "and its address must not be reclaimable"
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE scope='presence' AND user_id=$1", PROJ)
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_fresh_presence_vetoes_a_takeover_even_if_the_seat_looks_stale(
    client, db_pool
):
    """Defence in depth: if the two clocks ever drift again, presence wins.

    A seat aged past grace is normally reclaimable. But if something is
    independently heartbeating at that address it is ALIVE, and handing its
    address to a newcomer would put two sessions on one seat.
    """
    await _clear(db_pool)
    a = (await _claim(client, "quiet-but-alive")).json()
    await _age_seat(db_pool, a["seat"], SEAT_GRACE_SECONDS + 600)
    await client.post("/memory/presence", json={
        "identity": a["seat"], "project": PROJ, "state": "running",
        "provider": "claude", "session_nonce": "n1",
    })
    # Re-age ONLY the seat row, simulating the two clocks disagreeing.
    await _age_seat(db_pool, a["seat"], SEAT_GRACE_SECONDS + 600)

    newcomer = (await _claim(client, "newcomer", host="otherbox")).json()
    assert newcomer["seat"] != a["seat"], (
        "a seat whose holder is independently heartbeating must not be reclaimed"
    )
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE scope='presence' AND user_id=$1", PROJ)
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_a_quiet_but_live_session_keeps_its_address(client, db_pool):
    """The steal that liveness-by-tool-activity makes possible.

    A session doing long uninterrupted work sends no heartbeats and is
    therefore indistinguishable from a dead one — and it is exactly the
    session you least want to disturb. Observed 2026-07-24: a live session
    quiet 4.8h during its own build, seat reading reclaimable while it was
    still listening.

    Takeover now needs the full grace window, which is set past any plausible
    quiet stretch rather than past a plausible pause. The old "same provider,
    same host, past the LIVE window" shortcut allowed this at ~10 minutes.
    """
    await _clear(db_pool)
    a = (await _claim(client, "heads-down")).json()
    # Quiet for hours — well past the old 2h grace and the old shortcut, but
    # inside the window a working session can plausibly occupy.
    await _age_seat(db_pool, a["seat"], 6 * 3600)

    # A newcomer matching provider AND host — what the shortcut used to admit.
    newcomer = (await _claim(client, "newcomer", host=None)).json()
    assert newcomer["seat"] != a["seat"], (
        "a session quiet for hours is not evidence of death; its address must hold"
    )
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_a_restart_with_a_stable_key_never_needs_reclamation(client, db_pool):
    """Why hardening reclamation is nearly free.

    Continuity is keyed on session_key and has NO age condition, so a launcher-
    spawned session restarting gets its own seat back however long it was
    away. Reclamation therefore only ever serves ordinal tidiness for a
    genuinely NEW session — which is why it can afford to be conservative.
    """
    await _clear(db_pool)
    a = (await _claim(client, "stable-key")).json()
    await _age_seat(db_pool, a["seat"], 30 * 24 * 3600)  # a month quiet
    back = (await _claim(client, "stable-key")).json()
    assert back["seat"] == a["seat"], "a stable key must reclaim its own seat at any age"
    assert back["is_new"] is False
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_a_split_key_does_not_burn_an_ordinal_on_every_claim(client, db_pool):
    """RUNAWAY (2026-07-26): identical input must not keep minting addresses.

    Once a duplicate-key split put two rows under one session_key, the
    continuity lookup — a bare fetchrow over a non-unique predicate — kept
    returning the OTHER process's row and never the row this process had just
    been handed. So every heartbeat fell through to allocation: -3, -4, -5, -6
    on byte-identical input. Live sessions changed address every beat.

    Newest-wins no longer PRODUCES a split, so the split is constructed
    directly here: rows left by an older build, or by the bug itself, still
    exist in live stores and must not send a session spinning.
    """
    await _clear(db_pool)
    a = (await _claim(client, "runaway", session_nonce="A")).json()
    dupe = f"{PROJ}-claude-2"
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO memories
                (namespace, key, value, scope, user_id, project,
                 tags, tags_search, search_text, embedding, metadata)
            SELECT namespace, $1, $2, scope, user_id, project,
                   tags, tags_search, $2, NULL,
                   jsonb_set(metadata, '{session_nonce}', '"B"')
            FROM memories
            WHERE scope = 'seat' AND project = $3 AND key = $4
            """,
            f"seat/{dupe}", dupe, PROJ, f"seat/{a['seat']}",
        )

    seats = [
        (await _claim(client, "runaway", session_nonce="B")).json()["seat"]
        for _ in range(5)
    ]
    assert set(seats) == {dupe}, (
        f"address moved on identical input — one process, five addresses: {seats}"
    )
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_a_session_owning_two_rows_gets_one_stable_address(client, db_pool):
    """OSCILLATION (2026-07-26): the state two live sessions were actually in.

    One process — one session_key, one nonce — owning two seat rows, because
    the split above had already happened. With no ORDER BY, whichever row came
    back was kept, so the address flipped between them from call to call and
    the session's bridge, watcher and replies each reported a different
    identity. The claim must be a pure function of its input, and the
    duplicate must be collapsed rather than left to flip again.
    """
    await _clear(db_pool)
    a = (await _claim(client, "osc", session_nonce="N")).json()
    dupe = f"{PROJ}-claude-2"
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO memories
                (namespace, key, value, scope, user_id, project,
                 tags, tags_search, search_text, embedding, metadata)
            SELECT namespace, $1, $2, scope, user_id, project,
                   tags, tags_search, $2, NULL, metadata
            FROM memories
            WHERE scope = 'seat' AND project = $3 AND key = $4
            """,
            f"seat/{dupe}", dupe, PROJ, f"seat/{a['seat']}",
        )

    seats = {
        (await _claim(client, "osc", session_nonce="N")).json()["seat"]
        for _ in range(6)
    }
    assert seats == {a["seat"]}, f"a live session's address oscillated: {seats}"

    async with db_pool.acquire() as conn:
        remaining = await conn.fetchval(
            "SELECT count(*) FROM memories WHERE scope = 'seat' AND project = $1",
            PROJ,
        )
    assert remaining == 1, "one session must end up holding exactly one seat"
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_collapsing_duplicates_never_frees_a_seat_holding_mail(client, db_pool):
    """R8 outranks tidiness, in the new collapse path too.

    Deleting a duplicate seat row un-allocates that address, so a stranger
    could be granted it and read mail meant for someone else. A duplicate with
    undelivered mail is therefore left alone to age out normally.
    """
    await _clear(db_pool)
    a = (await _claim(client, "mailguard", session_nonce="N")).json()
    dupe = f"{PROJ}-claude-2"
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO memories
                (namespace, key, value, scope, user_id, project,
                 tags, tags_search, search_text, embedding, metadata)
            SELECT namespace, $1, $2, scope, user_id, project,
                   tags, tags_search, $2, NULL, metadata
            FROM memories
            WHERE scope = 'seat' AND project = $3 AND key = $4
            """,
            f"seat/{dupe}", dupe, PROJ, f"seat/{a['seat']}",
        )
        await conn.execute(
            """
            INSERT INTO memories
                (namespace, key, value, scope, user_id, project,
                 tags, tags_search, search_text, embedding, metadata)
            VALUES ($1, $2, 'held mail', 'inbox', $3, $4,
                    '', '', 'held mail', NULL, '{}'::jsonb)
            """,
            INBOX_NAMESPACE, f"inbox/{dupe}-pending", dupe, PROJ,
        )

    (await _claim(client, "mailguard", session_nonce="N")).json()

    async with db_pool.acquire() as conn:
        still_there = await conn.fetchval(
            "SELECT count(*) FROM memories WHERE scope = 'seat' AND project = $1 "
            "AND key = $2",
            PROJ, f"seat/{dupe}",
        )
    assert still_there == 1, "a duplicate holding undelivered mail must not be freed"
    await _clear(db_pool)


# --- ID-2: a runtime seat is REGISTERED, not fought over ---------------------

@pytest.mark.asyncio
async def test_a_runtime_seat_moves_the_registration(client, db_pool):
    """ID-2, the fix AgentBeast asked for: REGISTER the runtime seat.

    memory_take_seat used to set the seat client-side while the registry kept
    the old record — so every heartbeat's continuity answer reverted the file
    the agent had just written. Two mechanisms answering "who is this
    session", the loser never told. Registering makes continuity return the
    seat the session is ACTUALLY on.
    """
    await _clear(db_pool)
    first = (await _claim(client, "cowork", session_nonce="N1")).json()
    assert first["seat"] == f"{PROJ}-claude"

    # the agent deliberately re-seats mid-session
    moved = (await _claim(
        client, "cowork", session_nonce="N1",
        preferred_seat=f"{PROJ}-audit", runtime_seat=True,
    )).json()
    assert moved["seat"] == f"{PROJ}-audit"
    assert moved["renamed_from"] == f"{PROJ}-claude"
    assert moved["warning"] is None

    # continuity now returns the runtime seat — this is the claim that used
    # to revert it (an ordinary heartbeat, runtime flag still set client-side)
    heartbeat = (await _claim(
        client, "cowork", session_nonce="N1",
        preferred_seat=f"{PROJ}-audit", runtime_seat=True,
    )).json()
    assert heartbeat["seat"] == f"{PROJ}-audit", (
        "the next heartbeat took the runtime seat back — the revert loop"
    )
    assert heartbeat["renamed_from"] is None

    # ...and survives a bridge restart that no longer knows it took one
    # (process memory dies; the flag lives on the row)
    restart = (await _claim(
        client, "cowork", session_nonce="N2",
        preferred_seat=f"{PROJ}-audit",
    )).json()
    assert restart["seat"] == f"{PROJ}-audit"

    # the old name was freed — a newcomer can have it
    fresh = (await _claim(client, "newcomer", session_nonce="X1")).json()
    assert fresh["seat"] == f"{PROJ}-claude"
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_a_runtime_seat_held_by_another_session_is_refused_loudly(
    client, db_pool
):
    """The other consistent outcome: refusal must be an ERROR the session can
    act on, never a no-op — a silent split (bridge on one name, registry on
    another) is worse than either consistent outcome."""
    await _clear(db_pool)
    (await _claim(client, "holder", session_nonce="H1",
                  preferred_seat=f"{PROJ}-audit")).json()

    refused = (await _claim(
        client, "mover", session_nonce="M1",
        preferred_seat=f"{PROJ}-audit", runtime_seat=True,
    )).json()
    # "mover" had no prior row, so allocation grants it a DIFFERENT name
    # rather than the taken one
    assert refused["seat"] != f"{PROJ}-audit"

    # now mover holds a seat and tries to rename onto the taken name:
    # continuity refuses and says so
    still = (await _claim(
        client, "mover", session_nonce="M1",
        preferred_seat=f"{PROJ}-audit", runtime_seat=True,
    )).json()
    assert still["seat"] == refused["seat"], "must keep the registered seat"
    assert still["warning"] and f"{PROJ}-audit" in still["warning"], (
        "a refused rename with no warning is the silent no-op ID-2 forbids"
    )
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_a_rename_never_frees_a_seat_holding_undelivered_mail(
    client, db_pool
):
    """R8 outranks tidiness on the rename path too: the old name keeps its
    row while mail addressed to it is undelivered, so a stranger can never be
    allocated an address with someone's unread mail on it."""
    await _clear(db_pool)
    old = (await _claim(client, "mailmove", session_nonce="R1")).json()["seat"]
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO memories
                (namespace, key, value, scope, user_id, project,
                 tags, tags_search, search_text, embedding, metadata)
            VALUES ($1, $2, 'queued', 'inbox', $3, $4,
                    '', '', 'queued', NULL, '{}'::jsonb)
            """,
            INBOX_NAMESPACE, "inbox/mailmove-pending", old, PROJ,
        )

    moved = (await _claim(
        client, "mailmove", session_nonce="R1",
        preferred_seat=f"{PROJ}-relabel", runtime_seat=True,
    )).json()
    assert moved["seat"] == f"{PROJ}-relabel"

    async with db_pool.acquire() as conn:
        kept = await conn.fetchval(
            "SELECT count(*) FROM memories WHERE scope = 'seat' "
            "AND project = $1 AND key = $2",
            PROJ, f"seat/{old}",
        )
    assert kept == 1, "old seat with undelivered mail must age out, not free"
    await _clear(db_pool)
