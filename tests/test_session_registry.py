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
async def test_a_released_name_holding_mail_is_not_handed_to_the_next_session(
    client, db_pool
):
    """R8 on the FREE path, which nothing guarded until 2026-08-13.

    The takeover guard only runs when a seat ROW still exists, and
    ``seat_release`` DELETEs the row without consulting the inbox — so the
    clean-shutdown path a launcher drives on every despawn freed a name and let
    the next claimant INSERT into it and read a stranger's mail. Inbox rows key
    on the ADDRESS STRING, not on the seat row, so dropping the row moved
    nothing.
    """
    await _clear(db_pool)
    a = (await _claim(client, "departing")).json()
    seat = a["seat"]
    send = await client.post("/memory/send", json={
        "to": seat, "subject": "for the departing session", "body": "private",
    })
    assert send.status_code == 200

    rel = await client.post("/session/release", json={
        "session_key": "departing", "project": PROJ,
    })
    assert rel.json()["released"] == seat  # the name IS freed, as designed

    b = (await _claim(client, "stranger", host="otherbox")).json()
    assert b["seat"] != seat, (
        "a stranger was granted a released name that still held mail — "
        "they would read it, because acks and listing key on the address"
    )
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_mail_the_previous_holder_already_read_still_parks_the_name(
    client, db_pool
):
    """The guard must match what a NEWCOMER sees, not what the old holder saw.

    ACKS ARE PER-READER: ``inbox_list`` hides a message only from readers in its
    own ``read_by``. So a message the previous holder read and acked stays
    ``open`` and is fully visible to whoever is allocated that name next. The
    original predicate asked for ``read_by = []`` and reported such an address
    CLEAR — answering a question nobody was asking.
    """
    await _clear(db_pool)
    a = (await _claim(client, "reader")).json()
    seat = a["seat"]
    send = await client.post("/memory/send", json={
        "to": seat, "subject": "read then abandoned", "body": "still private",
    })
    msg_id = send.json()["id"]
    # The holder reads it — and then the session dies.
    ack = await client.post(f"/memory/inbox/{msg_id}/ack",
                            json={"reader_identity": seat})
    assert ack.status_code == 200
    await client.post("/session/release", json={
        "session_key": "reader", "project": PROJ,
    })

    b = (await _claim(client, "stranger-2", host="otherbox")).json()
    assert b["seat"] != seat, (
        "granted a name whose mail was merely ACKED by the dead holder — "
        "per-reader acks mean the newcomer still sees every one of those rows"
    )
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_parking_a_distinctive_preferred_name_is_loud(client, db_pool):
    """GRANT-1(a): exiling a session from its own name must not be silent.

    The field chain, twice live (2026-08-16 "two Beast Chats", 2026-08-17
    "AB vs AB-App"): a launcher injects a distinctive preferred seat, the name
    holds open mail, R8 parks it, and the claim fell to a project-lane ordinal
    with warning:null — the team's session lost the only string that
    distinguished it and nobody was told. The parking stays (R8 is correct);
    the grant must now NAME the parked address, the reason, and the drain path.
    """
    await _clear(db_pool)
    name = f"{PROJ}-app-claude"
    a = (await _claim(client, "app-session-1", preferred_seat=name)).json()
    assert a["seat"] == name and a["warning"] is None
    send = await client.post("/memory/send", json={
        "to": name, "subject": "work order", "body": "parked with the name",
    })
    assert send.status_code == 200
    await client.post("/session/release", json={
        "session_key": "app-session-1", "project": PROJ,
    })

    b = (await _claim(client, "app-session-2", preferred_seat=name)).json()
    assert b["seat"] != name  # R8 held — that is not the defect
    warning = b["warning"] or ""
    assert "preferred_seat_parked" in warning
    assert name in warning          # names the parked address
    assert "open mail" in warning   # names the reason
    assert b["seat"] in warning     # names what was granted instead
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_base_name_preference_falling_to_ordinal_stays_quiet(
    client, db_pool
):
    """The base name is a convention, not an identity — no wolf-crying.

    Every launcher computes ``<project>-<provider>`` for every session, so a
    second session preferring the base and landing on ``-2`` is ordinary
    allocation (a colleague holds the base), not an exile. Warning on each
    such claim would bury the GRANT-1 signal under noise.
    """
    await _clear(db_pool)
    base = f"{PROJ}-claude"
    a = (await _claim(client, "first", preferred_seat=base)).json()
    assert a["seat"] == base
    b = (await _claim(client, "second", preferred_seat=base)).json()
    assert b["seat"] == f"{base}-2"
    assert b["warning"] is None
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_resolved_mail_does_not_park_a_name_forever(client, db_pool):
    """The guard protects what a newcomer would SEE, so it must let go too.

    Resolved mail has drained from the default view for every reader, so it
    cannot leak to the next holder — parking the name on it would be R8
    outranking R7 for no gain, and names would silently accrete.
    """
    await _clear(db_pool)
    a = (await _claim(client, "closer")).json()
    seat = a["seat"]
    send = await client.post("/memory/send", json={
        "to": seat, "subject": "loop closed", "body": "done",
    })
    msg_id = send.json()["id"]
    res = await client.post(f"/memory/inbox/{msg_id}/resolve",
                            json={"reader_identity": seat})
    assert res.status_code == 200
    await client.post("/session/release", json={
        "session_key": "closer", "project": PROJ,
    })

    b = (await _claim(client, "newcomer-3", host="otherbox")).json()
    assert b["seat"] == seat, (
        "resolved mail is invisible to a newcomer, so it must not park the name"
    )
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
async def test_seats_endpoint_exports_the_number_not_a_threshold(client, db_pool):
    """The registry answers addressing questions, not liveness ones.

    Both `is_live` and `reclaimable` were removed 2026-08-01. Each was a
    threshold applied to `age_seconds` and nothing else, so exporting them sent
    the same bit twice — once as a fact, once as a verdict. The verdict half is
    what invited consumers to adopt our threshold as their policy.
    """
    await _clear(db_pool)
    a = (await _claim(client, "visible")).json()
    r = await client.post("/session/seats", json={"project": PROJ})
    entry = [s for s in r.json()["seats"] if s["seat"] == a["seat"]][0]
    assert "is_live" not in entry, "the registry is back to rendering verdicts"
    assert "reclaimable" not in entry, "our threshold is a consumer's policy again"
    assert entry["age_seconds"] is not None  # the fact a caller judges from
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
    read past the backstop — at which point a newcomer could be granted an
    address a running session still held.

    2026-08-01: no signal is REQUIRED to hold an address (an address must
    outlive its owner being awake), but a heartbeat still pushes the backstop
    out, which is what this pins. The assertion moved from the removed
    `reclaimable` flag to the number consumers now judge from.
    """
    await _clear(db_pool)
    a = (await _claim(client, "beating")).json()
    # Age the seat past the backstop, as a long-quiet session's would.
    await _age_seat(db_pool, a["seat"], SEAT_GRACE_SECONDS + 600)

    listed = (await client.post("/session/seats",
                                json={"session_key": "beating"})).json()["seats"][0]
    assert listed["age_seconds"] >= SEAT_GRACE_SECONDS  # past the backstop

    # One heartbeat at that identity — exactly what a live session sends.
    beat = await client.post("/memory/presence", json={
        "identity": a["seat"], "project": PROJ, "state": "running",
        "provider": "claude", "session_nonce": "n1",
    })
    assert beat.status_code == 200

    after = (await client.post("/session/seats",
                               json={"session_key": "beating"})).json()["seats"][0]
    assert after["age_seconds"] < SEAT_GRACE_SECONDS, (
        "a heartbeat must refresh the seat row — a session that is still "
        "speaking must never have its address reissued"
    )
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


@pytest.mark.asyncio
async def test_continuity_holds_even_when_the_seat_row_is_stale_past_grace(
    client, db_pool
):
    """SEAT-16's unverified claim, now measured: continuity has NO age gate.

    The claim path documents idempotency on session_key as unconditional —
    a one-way door, not a liveness window — but until this test nobody had
    proven that a row stale past the full 7d grace window still grants its
    key the same seat back. It must: grace exists for STRANGERS taking over
    an abandoned name, never for a returning key, and a returning session
    that lost its address because it was quiet too long would be the exact
    stolen-address failure the asymmetry rule forbids.
    """
    await _clear(db_pool)
    first = (await _claim(client, "longsleep", session_nonce="N1")).json()
    await _age_seat(db_pool, first["seat"], SEAT_GRACE_SECONDS * 2)

    back = (await _claim(client, "longsleep", session_nonce="N1")).json()
    assert back["seat"] == first["seat"], (
        "a stale-past-grace row must still grant its own key continuity"
    )
    assert back["is_new"] is False
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_seats_payload_marks_generated_keys_as_a_fact(client, db_pool):
    """SEAT-16: a consumer must be able to tell a process-derived key from a
    launcher-injected one when reading a seat back.

    A generated key (`auto-` prefix) names a harness PROCESS, so it changes
    on every revive of a revivable session — the docstring used to promise
    "the same guarantees" as an injected key, and a consumer inherited that
    assumption into an ordinal pileup (measured 2026-08-10). The payload now
    serves the minting as a fact; the verdict stays the consumer's.
    """
    await _clear(db_pool)
    await _claim(client, "auto-testhost-4242-mon-jan-1-00-00-00-2026")
    await _claim(client, "cursor-thread-abc123")

    r = await client.post("/session/seats", json={"project": PROJ})
    assert r.status_code == 200
    by_key = {s["session_key"]: s for s in r.json()["seats"]}

    generated = by_key["auto-testhost-4242-mon-jan-1-00-00-00-2026"]
    injected = by_key["cursor-thread-abc123"]
    assert generated["session_key_generated"] is True
    assert injected["session_key_generated"] is False
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_seats_serve_watcher_beat_in_the_rosters_vocabulary(
    client, db_pool
):
    """SEATS-1 (requested by AgentBeast 2026-07-28): a picker for sessions on
    OTHER boxes needs per-seat wake-ability from the seats payload it already
    reads. Three-valued, exactly as the roster serves it: True = a watcher
    beat within the freshness window; False = one has beaten here and went
    quiet; None = no watcher has EVER beaten — and None is never coerced to
    False, because absent is not dead (the conflation that once let a live
    session's address be taken)."""
    await _clear(db_pool)
    from datetime import datetime, timedelta, timezone

    fresh = (await _claim(client, "eared", session_nonce="E1")).json()["seat"]
    quiet = (await _claim(client, "deafened", session_nonce="E2")).json()["seat"]
    never = (await _claim(client, "earless", session_nonce="E3")).json()["seat"]

    now = datetime.now(timezone.utc)
    async with db_pool.acquire() as conn:
        for seat, seen in (
            (fresh, now.isoformat()),
            (quiet, (now - timedelta(hours=3)).isoformat()),
        ):
            await conn.execute(
                """
                INSERT INTO memories
                    (namespace, key, value, scope, user_id, project,
                     tags, tags_search, search_text, embedding, metadata)
                VALUES ($1, $2, 'running', 'presence', $3, $3,
                        '', '', '', NULL, $4::jsonb)
                """,
                INBOX_NAMESPACE, f"presence/{seat}", PROJ,
                f'{{"watcher_last_seen": "{seen}"}}',
            )
        # `never` gets a presence row with NO watcher_last_seen at all
        await conn.execute(
            """
            INSERT INTO memories
                (namespace, key, value, scope, user_id, project,
                 tags, tags_search, search_text, embedding, metadata)
            VALUES ($1, $2, 'running', 'presence', $3, $3, '', '', '', NULL,
                    '{}'::jsonb)
            """,
            INBOX_NAMESPACE, f"presence/{never}", PROJ,
        )

    r = await client.post("/session/seats", json={"project": PROJ})
    assert r.status_code == 200
    by_seat = {s["seat"]: s for s in r.json()["seats"]}

    assert by_seat[fresh]["watcher_alive"] is True
    assert by_seat[fresh]["watcher_last_seen"] is not None
    assert by_seat[quiet]["watcher_alive"] is False
    assert by_seat[never]["watcher_alive"] is None, (
        "no beat ever must read None, not False — absent is not dead"
    )
    assert by_seat[never]["watcher_last_seen"] is None

    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope = 'presence' AND user_id = $1",
            PROJ,
        )
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_seat_without_presence_row_reads_watcher_none(client, db_pool):
    """A seat whose session never heartbeat presence at all (claimed via the
    registry, then silent) must read None/None — the join is LEFT, and a
    missing row is 'no basis', not 'dead'."""
    await _clear(db_pool)
    seat = (await _claim(client, "rowless", session_nonce="R1")).json()["seat"]
    r = await client.post("/session/seats", json={"project": PROJ})
    entry = {s["seat"]: s for s in r.json()["seats"]}[seat]
    assert entry["watcher_alive"] is None
    assert entry["watcher_last_seen"] is None
    await _clear(db_pool)


# --- LANE-1: lane-string reservation (docs/design/immortal-addresses.md) ----


@pytest.fixture
def lanes_on(monkeypatch):
    from server.config import settings
    monkeypatch.setattr(settings, "lane_reservation_enabled", True)


@pytest.mark.asyncio
async def test_flag_off_is_todays_behavior(client, db_pool):
    """Default OFF: the base string is granted exactly as before LANE-1.
    Migration order is drain -> new bridges -> reserve; until the flip this
    code must be invisible."""
    await _clear(db_pool)
    r = await _claim(client, "laneoff-1")
    assert r.json()["seat"] == f"{PROJ}-claude"
    assert r.json()["warning"] is None


@pytest.mark.asyncio
async def test_reserved_lane_is_never_minted(client, db_pool, lanes_on):
    """With reservation on, the lane string is skipped: first occupant gets
    <base>-2, and the lane remains an address with no seat row."""
    await _clear(db_pool)
    r = await _claim(client, "laneres-1")
    assert r.json()["seat"] == f"{PROJ}-claude-2"
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM memories WHERE scope='seat' AND project=$1 AND key=$2",
            PROJ, f"seat/{PROJ}-claude",
        )
    assert row is None


@pytest.mark.asyncio
async def test_old_bridge_preferring_the_lane_is_degraded_loud(
    client, db_pool, lanes_on
):
    """Gate-(e) safety net: an old bridge injecting the lane as its identity
    gets a WORKING allocated occupant plus an explicit lane_reserved notice —
    never silently unseated, never a spawn error (reviewer condition, v3)."""
    await _clear(db_pool)
    r = await _claim(client, "oldbridge-1", preferred_seat=f"{PROJ}-claude")
    body = r.json()
    assert body["seat"] == f"{PROJ}-claude-2"
    assert body["warning"] and "lane_reserved" in body["warning"]


@pytest.mark.asyncio
async def test_runtime_reseat_onto_a_lane_is_refused(client, db_pool, lanes_on):
    """memory_take_seat is exact-or-refused; a lane is never grantable. The
    session keeps its seat and gets an actionable refusal, not a redirect."""
    await _clear(db_pool)
    first = (await _claim(client, "rt-1")).json()["seat"]
    r = await _claim(client, "rt-1", preferred_seat=f"{PROJ}-grok",
                     runtime_seat=True)
    body = r.json()
    assert body["seat"] == first
    assert body["warning"] and "lane_reserved" in body["warning"]
    assert body.get("renamed_from") is None


@pytest.mark.asyncio
async def test_takeover_never_remints_a_lane_named_corpse(
    client, db_pool, lanes_on
):
    """A pre-reservation corpse still sitting on the lane string must not be
    taken over as an occupant seat — same defect through the other door. The
    newcomer allocates past it; the corpse ages out / is drained."""
    await _clear(db_pool)
    from server.config import settings
    monkeypatch_off = settings.lane_reservation_enabled
    settings.lane_reservation_enabled = False
    try:
        r0 = await _claim(client, "corpse-1")
        assert r0.json()["seat"] == f"{PROJ}-claude"
    finally:
        settings.lane_reservation_enabled = monkeypatch_off
    await _age_seat(db_pool, f"{PROJ}-claude", SEAT_GRACE_SECONDS + 3600)
    r = await _claim(client, "newcomer-1")
    body = r.json()
    assert body["seat"] == f"{PROJ}-claude-2"
    assert body["reclaimed_from"] is None


@pytest.mark.asyncio
async def test_admin_is_exempt_from_lanes(client, db_pool, lanes_on):
    """No admin-<provider> lanes, ever (SEAT-ADMIN-1): admin is one shared
    role and reservation must not touch it."""
    r = await client.post("/session/claim", json={
        "session_key": "adm-lane-1", "project": "admin", "provider": "claude",
        "preferred_seat": "admin",
    })
    assert r.json()["seat"] == "admin"


# --- LANE-4: death certificates + lane-cursor succession --------------------


async def _mail(db_pool, to, mid, read_by=None, project=PROJ):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO memories (namespace, key, value, scope, user_id,
                tags, tags_search, search_text, embedding, metadata)
            VALUES ('fleet', $1, 'b', 'inbox', $2, '', '', 'b', NULL, $3::jsonb)
            ON CONFLICT DO NOTHING
            """,
            mid, to,
            __import__("json").dumps(
                {"kind": "inbox", "from": "peer@x", "subject": "s",
                 "status": "open", "archived": False,
                 "read_by": read_by or []}),
        )


async def _read_by(db_pool, mid):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT metadata->'read_by' AS rb FROM memories WHERE key=$1", mid)
    import json as _j
    return _j.loads(row["rb"]) if row and row["rb"] else []


def _cert(client, **kw):
    body = {"session_key": "dead-1", "seat": f"{PROJ}-claude",
            "lane": f"{PROJ}-claude", "project": PROJ, "provider": "claude",
            "host": "macmini", "died_at": "2026-08-14T12:00:00Z",
            "cause": "stop"}
    body.update(kw)
    return client.post("/session/death", json=body)


async def _clear_lane4(db_pool):
    await _clear(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope IN ('death','lane_cursor') "
            "AND project = $1", PROJ)


@pytest.mark.asyncio
async def test_cert_is_idempotent_and_harvests_the_dead_readers_acks(
    client, db_pool
):
    await _clear_lane4(db_pool)
    # the dead occupant read lane+project mail; its own seat-DM must not
    # enter the cursor, nor machine: mail
    await _mail(db_pool, f"{PROJ}-claude", "inbox/l1",
                read_by=[f"{PROJ}-claude@macmini"])
    await _mail(db_pool, PROJ, "inbox/p1",
                read_by=[f"{PROJ}-claude@macmini"])
    await _mail(db_pool, f"{PROJ}-claude", "inbox/dm1",
                read_by=[f"{PROJ}-claude@macmini"])
    r = await _cert(client)
    body = r.json()
    assert r.status_code == 200 and body["created"] is True
    assert body["cursor_updated"] is True
    # dm1 is TO the dead seat and excluded; l1 was also TO the seat string
    # (pre-reservation lane==seat) — excluded the same way; p1 survives
    assert body["cursor_size"] == 1
    r2 = await _cert(client)
    assert r2.json()["created"] is False, "repeat POST is a no-op create"
    assert r2.json()["cursor_size"] == 1


@pytest.mark.asyncio
async def test_cert_accepted_while_presence_looks_live(client, db_pool):
    """PICK-REG-1b: the certificate wins over a still-beating heartbeat —
    a live-looking presence row is never a reason to reject."""
    await _clear_lane4(db_pool)
    await _claim(client, "victim-1")
    r = await _cert(client, session_key="victim-1")
    assert r.status_code == 200 and r.json()["created"] is True


@pytest.mark.asyncio
async def test_empty_seat_stores_cert_but_never_harvests(client, db_pool):
    """PM ruling: no seat → no reader identity → skip harvest entirely;
    do NOT match seat@% from an empty seat."""
    await _clear_lane4(db_pool)
    r = await _cert(client, seat="", session_key="k-empty")
    body = r.json()
    assert r.status_code == 200 and body["created"] is True
    assert body["cursor_updated"] is False and body["cursor_size"] == 0


@pytest.mark.asyncio
async def test_seat6_fallback_idempotency_key(client, db_pool):
    """Empty session_key (grok start path): (seat, died_at) is the key —
    and the cursor still updates."""
    await _clear_lane4(db_pool)
    await _mail(db_pool, PROJ, "inbox/p2",
                read_by=[f"{PROJ}-grok@macmini"])
    kw = dict(session_key="", seat=f"{PROJ}-grok", lane=f"{PROJ}-grok",
              provider="grok")
    r = await _cert(client, **kw)
    assert r.json()["created"] is True and r.json()["cursor_updated"] is True
    assert (await _cert(client, **kw)).json()["created"] is False


@pytest.mark.asyncio
async def test_both_keys_absent_is_422(client, db_pool):
    r = await _cert(client, session_key="", seat="")
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_first_occupant_seat_equals_lane_passes_while_flag_off(
    client, db_pool
):
    """PM amendment: pre-reservation the granted occupant IS the lane
    string — an honest cert must not be rejected. (With the flag on the
    same equality is the seat_for() trap and 422s.)"""
    await _clear_lane4(db_pool)
    r = await _cert(client, session_key="k-first")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_lane_as_seat_rejected_only_under_reservation(
    client, db_pool, lanes_on
):
    r = await _cert(client, session_key="k-trap")
    assert r.status_code == 422
    assert "lane_as_seat" in r.json()["detail"]


@pytest.mark.asyncio
async def test_successor_inherits_cursor_on_empty_lane(client, db_pool):
    """Succession: predecessor certified dead, lane empty → the new
    occupant's read-state materializes the cursor (existing read semantics,
    recorded on the cursor row for forensics)."""
    await _clear_lane4(db_pool)
    await _mail(db_pool, PROJ, "inbox/p3",
                read_by=[f"{PROJ}-claude@macmini"])
    await _cert(client, session_key="old-1")
    # predecessor's seat row released (clean stop)
    await client.post("/session/release", json={
        "session_key": "old-1", "project": PROJ})
    r = await _claim(client, "new-1", host="macmini")
    seat = r.json()["seat"]
    rb = await _read_by(db_pool, "inbox/p3")
    assert f"{seat}@macmini" in rb, "successor inherited the lane cursor"


@pytest.mark.asyncio
async def test_live_colleague_blocks_inheritance(client, db_pool):
    """Problem-1 rule: a live colleague on the lane means the new reader
    starts with EMPTY personal acks — inheriting would steal unread mail
    from a living session."""
    await _clear_lane4(db_pool)
    await _mail(db_pool, PROJ, "inbox/p4",
                read_by=[f"{PROJ}-claude@macmini"])
    # colleague occupies the lane with fresh presence
    c = await _claim(client, "alive-1", host="macmini")
    alive_seat = c.json()["seat"]
    await client.post("/memory/presence", json={
        "identity": alive_seat, "project": PROJ})
    await _cert(client, session_key="old-2", seat=f"{PROJ}-claude-9")
    r = await _claim(client, "new-2", host="macmini")
    seat = r.json()["seat"]
    rb = await _read_by(db_pool, "inbox/p4")
    assert f"{seat}@macmini" not in rb, "live colleague must block inheritance"
