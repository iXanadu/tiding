"""ADDR-REG: GET /session/addresses — every held name, and why.

The register exists because the roster only shows who is SPEAKING: corpses
inside the 7d grace window, names parked by undrained mail (with or without a
seat row), and lost preferred-name requests were all invisible, and the owner
had to reconstruct them by archaeology (2026-08-17, huddle UhAND1ey).
"""

import json
import uuid

import pytest

from server.services.memory_service import INBOX_NAMESPACE
from server.services.session_registry import (
    SEAT_GRACE_SECONDS,
    SEAT_LIVE_SECONDS,
)

PROJ = "addrregtest"


async def _clear(db_pool, project=PROJ):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope IN ('seat', 'death') AND project = $1",
            project,
        )
        await conn.execute(
            "DELETE FROM memories WHERE scope = 'inbox' AND user_id LIKE $1",
            f"{project}%",
        )


async def _age_seat(db_pool, seat, seconds, project=PROJ):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE memories SET last_used_at = NOW() - ($1 || ' seconds')::interval
            WHERE scope = 'seat' AND project = $2 AND key = $3
            """,
            str(seconds), project, f"seat/{seat}",
        )


async def _open_mail(db_pool, addr):
    """Plant one open inbox row on an address — the R8 park predicate's set."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO memories
                (namespace, key, value, scope, user_id, project,
                 tags, tags_search, search_text, embedding, metadata)
            VALUES ($1, $2, 'test mail', 'inbox', $3, NULL,
                    '', '', 'test mail', NULL, $4::jsonb)
            """,
            INBOX_NAMESPACE, f"inbox/{uuid.uuid4()}", addr,
            json.dumps({"status": "open"}),
        )


def _claim(client, key, **kw):
    body = {"session_key": key, "project": PROJ, "provider": "claude"}
    body.update(kw)
    return client.post("/session/claim", json=body)


async def _register(client, project=PROJ):
    r = await client.get("/session/addresses", params={"project": project})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    return {e["address"]: e for e in body["entries"]}


@pytest.mark.asyncio
async def test_preferred_seat_recorded_on_fallback_grant(client, db_pool):
    """The 2026-08-17 incident: wanted agentbeast-app-grok, got grok-6.

    The register must serve WHAT WAS ASKED next to WHAT WAS GRANTED — losing
    the request in a transient claim warning is what made the ordinal
    illegible to the owner.
    """
    await _clear(db_pool)
    distinctive = f"{PROJ}-app-claude"
    r = await _claim(client, "holder", preferred_seat=distinctive)
    assert r.json()["seat"] == distinctive

    # Second session wants the SAME distinctive name while the holder is live:
    # falls back to the lane base, and the row must remember the ask.
    r = await _claim(client, "loser", preferred_seat=distinctive)
    granted = r.json()["seat"]
    assert granted == f"{PROJ}-claude"

    reg = await _register(client)
    assert reg[granted]["preferred_seat"] == distinctive
    assert reg[granted]["entry_type"] == "seat"
    assert reg[granted]["claimed_at"] is not None
    # The passed-over name reads live-holder — the allocator's own reason.
    assert reg[distinctive]["allocation"] == {
        "would_skip": True, "reason": "live-holder", "grace_expires_at": None,
    }
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_grace_window_corpse_serves_expiry(client, db_pool):
    """A corpse inside the 7d grace window must say so, with the expiry.

    "Why is this name held and when does it free" was unanswerable on
    2026-08-17 without reading the allocator's source.
    """
    await _clear(db_pool)
    seat = (await _claim(client, "corpse")).json()["seat"]
    await _age_seat(db_pool, seat, SEAT_LIVE_SECONDS + 3600)

    reg = await _register(client)
    entry = reg[seat]
    assert entry["allocation"]["would_skip"] is True
    assert entry["allocation"]["reason"] == "grace-window"
    assert entry["allocation"]["grace_expires_at"] is not None
    assert entry["watcher_alive"] is None  # never beaten ≠ dead
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_mail_parks_a_seatless_name_and_the_register_shows_it(
        client, db_pool):
    """The R8 class invisible to /session/seats: no row, open mail, parked."""
    await _clear(db_pool)
    ghost = f"{PROJ}-claude-9"
    await _open_mail(db_pool, ghost)

    reg = await _register(client)
    entry = reg[ghost]
    assert entry["entry_type"] == "mail-only"
    assert entry["undrained_mail_count"] == 1
    assert entry["allocation"]["reason"] == "mail-parked"
    assert entry["project"] is None  # an inbox address is a bare string
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_expired_corpse_with_mail_reads_mail_parked(client, db_pool):
    """Past grace, holding mail: the mail is now the reason the name waits."""
    await _clear(db_pool)
    seat = (await _claim(client, "dead-with-mail")).json()["seat"]
    await _open_mail(db_pool, seat)
    await _age_seat(db_pool, seat, SEAT_GRACE_SECONDS + 3600)

    reg = await _register(client)
    entry = reg[seat]
    assert entry["undrained_mail_count"] == 1
    assert entry["allocation"]["reason"] == "mail-parked"
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_free_name_reads_free(client, db_pool):
    """Past grace, no mail, no presence: would_skip false — claimable."""
    await _clear(db_pool)
    seat = (await _claim(client, "long-dead")).json()["seat"]
    await _age_seat(db_pool, seat, SEAT_GRACE_SECONDS + 3600)

    reg = await _register(client)
    assert reg[seat]["allocation"] == {
        "would_skip": False, "reason": None, "grace_expires_at": None,
    }
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_lane_named_row_reads_reserved_when_reservation_on(
        client, db_pool, monkeypatch):
    """Audit residual (grok-5, 2026-08-17): with reservation ON a lane-named
    row must never read claimable, whatever its age — seat_claim skips lanes
    before every other check, so the register must too."""
    from server.config import settings

    await _clear(db_pool)
    # Pre-reservation honest first occupant: granted the lane string itself.
    seat = (await _claim(client, "first")).json()["seat"]
    assert seat == f"{PROJ}-claude"
    await _age_seat(db_pool, seat, SEAT_GRACE_SECONDS + 3600)  # past grace

    reg = await _register(client)
    assert reg[seat]["allocation"]["would_skip"] is False  # reservation off

    monkeypatch.setattr(settings, "lane_reservation_enabled", True)
    reg = await _register(client)
    assert reg[seat]["allocation"] == {
        "would_skip": True, "reason": "reserved-lane",
        "grace_expires_at": None,
    }
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_death_certificate_attaches_by_session_key(client, db_pool):
    """A spawner's cert is the stopped-at evidence; it must ride the row —
    INCLUDING while the row still looks live (PICK-REG-1b: a heartbeat can
    outlive a kill, it can never observe one).

    The timeline must be coherent: died_at AFTER the row's last beat. The
    original fixture certified death YESTERDAY on a row that beat TODAY —
    contradictory facts that REG-DEATH-1's life-after-death voiding now
    correctly rejects (that shape is a reused-key successor, not a corpse).
    """
    from datetime import datetime, timedelta, timezone

    await _clear(db_pool)
    seat = (await _claim(client, "certified")).json()["seat"]
    died_at = (datetime.now(timezone.utc)
               + timedelta(seconds=2)).isoformat()
    r = await client.post("/session/death", json={
        "session_key": "certified",
        "seat": seat,
        "project": PROJ,
        "provider": "claude",
        "died_at": died_at,
        "cause": "stop",
        "graceful": True,
    })
    assert r.status_code == 200

    reg = await _register(client)
    entry = reg[seat]
    # The row is seconds old — it LOOKS live — and the cert still rides.
    assert entry["allocation"]["reason"] == "live-holder"
    assert entry["death_certified"] is True
    assert entry["death"]["cause"] == "stop"
    assert entry["death"]["graceful"] is True
    assert entry["death"]["died_at"] == died_at
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_reused_key_successor_is_not_pinned_with_its_predecessors_cert(client, db_pool):
    """REG-DEATH-1 (shipped 616140a, pinned here): a launcher's slot-derived
    session_key SURVIVES respawns, so a predecessor's cert lands under the
    name's CURRENT holder's key. Evidence of life AFTER died_at — here the
    successor's own claim — voids the cert on the register; it must not read
    a live seat as dead off a corpse that shares its key."""
    from datetime import datetime, timedelta, timezone

    await _clear(db_pool)
    seat = (await _claim(client, "reusedkey")).json()["seat"]
    # A cert for the PREVIOUS incarnation: it died an hour ago, under the
    # same key, and this claim happened after that.
    died_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    r = await client.post("/session/death", json={
        "session_key": "reusedkey", "seat": seat, "project": PROJ,
        "provider": "claude", "died_at": died_at, "cause": "stop",
        "graceful": True,
    })
    assert r.status_code == 200
    entry = (await _register(client))[seat]
    assert entry["allocation"]["reason"] == "live-holder"
    assert entry["death"] is None, "a claim after died_at must void the cert"
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_project_filter_and_channel_exclusion(client, db_pool):
    """?project= narrows; '#'-channels never appear (not allocatable names)."""
    await _clear(db_pool)
    seat = (await _claim(client, "filtered")).json()["seat"]
    await _open_mail(db_pool, f"{PROJ}-cursor")   # prefix-matched mail-only
    await _open_mail(db_pool, "#addrregchannel")  # excluded always

    reg = await _register(client)
    assert seat in reg
    assert f"{PROJ}-cursor" in reg
    assert "#addrregchannel" not in reg
    # A different project's register must not see this project's names.
    other = await _register(client, project="someotherproject")
    assert seat not in other
    assert f"{PROJ}-cursor" not in other

    # Cleanup the channel row too (it matches no PROJ% pattern).
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope = 'inbox' AND user_id = $1",
            "#addrregchannel",
        )
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_exempt_role_mail_reads_exempt_not_mail_parked(client, db_pool):
    """'admin' is deliberately shared; allocation never runs its ladder on it.

    Labeling its open mail "mail-parked" implied the mail was what blocked
    allocation — on a name allocation does not apply to at all (the noise the
    outcomes review named on admin/owner rows).
    """
    await _open_mail(db_pool, "admin")
    await _open_mail(db_pool, "admin@webone")

    r = await client.get("/session/addresses")
    assert r.status_code == 200
    reg = {e["address"]: e for e in r.json()["entries"]}
    for addr in ("admin", "admin@webone"):
        assert reg[addr]["allocation"] == {
            "would_skip": True, "reason": "exempt-role",
            "grace_expires_at": None,
        }, addr

    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope = 'inbox' AND user_id LIKE 'admin%'",
        )


@pytest.mark.asyncio
async def test_human_principal_address_reads_person_address(client, db_pool):
    """A person's name is not in any seat-candidate space — say so.

    The owner's address holding mail is normal correspondence, not a parked
    allocation; 'mail-parked' on it sent readers hunting a defect that is
    not there.
    """
    person = "regtesthuman"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO principals (name, type) VALUES ($1, 'human') "
            "ON CONFLICT (name) DO UPDATE SET type = 'human', active = TRUE",
            person,
        )
    await _open_mail(db_pool, person)

    r = await client.get("/session/addresses")
    reg = {e["address"]: e for e in r.json()["entries"]}
    assert reg[person]["entry_type"] == "mail-only"
    assert reg[person]["allocation"]["reason"] == "person-address"
    assert reg[person]["allocation"]["would_skip"] is True

    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope = 'inbox' AND user_id = $1",
            person,
        )
        await conn.execute("DELETE FROM principals WHERE name = $1", person)


@pytest.mark.asyncio
async def test_runtime_move_records_the_taken_name_as_preferred(
        client, db_pool):
    """A deliberate rename must not erase the row's grant-time ask.

    Measured live 2026-08-18: agentbeast-app-grok's runtime-moved row served
    preferred_seat null — UNRECORDED — while the abandoned ordinal row kept
    the ask. The taken name IS the ask; the moved row records it.
    """
    await _clear(db_pool)
    nonce = "move-nonce-1"
    r = await _claim(client, "mover", session_nonce=nonce)
    first = r.json()["seat"]

    taken = f"{PROJ}-audit"
    r = await _claim(client, "mover", session_nonce=nonce,
                     runtime_seat=True, preferred_seat=taken)
    assert r.json()["seat"] == taken
    assert r.json()["renamed_from"] == first

    reg = await _register(client)
    assert reg[taken]["runtime"] is True
    assert reg[taken]["preferred_seat"] == taken
    await _clear(db_pool)
