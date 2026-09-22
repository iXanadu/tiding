"""SEAT-13: a certified goodbye frees the chair. The owner's symptom, end to end.

HIS WORDS (2026-09-22): "I run 8 hrs, determine context needs refreshing and I
exit the session. 2 minutes later I spawn the same session again, all I want is
a new context. Almost 100% of the time I get a <name>-n, and n is weird, could
be 1, or 10."

THE MECHANISM. The bridge files a death certificate on every clean exit
(EXIT-NOTICE-1) and the roster renders it as EXITED. But `allocation_decision`
had no rung for "we were told it died", so a returning session fell through to
rungs that read only `age`:

    60s after a clean exit   -> live-holder    (age < SEAT_LIVE_SECONDS)
   120s  <- the owner's case -> live-holder
   601s                      -> grace-window   (for the next 7 days)

⚠️ THIS FILE CORRECTED ITS AUTHOR. I told the owner the 7-day backstop blocked
his 2-minute restart. It does not: at 120s the store still believes the session
he just exited is WORKING. Both rungs read `last_used_at` — the field a WATCHER
refreshes (LAST-SPOKE-IS-THE-WATCHER-1) — so a dead session looks alive for ten
minutes on its own watcher's dying beats, then recently-departed for a week.
A rung placed below those two would never fire for the case it exists to serve,
which is why the fix sits ABOVE them.

THE SAFETY PROPERTY, which is the whole reason this is shippable while two
teams sit stalled: the rung fires on a CERTIFICATE and never on silence, and it
can only free a chair with NO mail waiting in it. An idle session, a blocked
session, a session waiting out an allocation reset has filed nothing — so it is
untouched. A certified-dead chair still holding mail parks exactly as before,
so a successor can never inherit a corpse's inbox.
"""

from datetime import datetime, timezone

import pytest

from server.services.memory_service import PRESENCE_NAMESPACE, get_pool
from server.services.session_registry import (
    SEAT_GRACE_SECONDS,
    SEAT_LIVE_SECONDS,
    allocation_decision,
    death_certify,
    seat_claim,
)

PROJECT = "seat13probe"


async def _cleanup():
    pool = await get_pool()
    async with pool.acquire() as conn:
        for scope in ("seat", "death", "presence"):
            await conn.execute(
                "DELETE FROM memories WHERE namespace = $1 AND scope = $2 "
                "AND (project = $3 OR user_id = $3)",
                PRESENCE_NAMESPACE, scope, PROJECT,
            )


# ── the ladder in isolation ─────────────────────────────────────────────────

def test_grace_window_is_seven_days():
    """Anchors the numbers the owner has been waiting out."""
    assert SEAT_GRACE_SECONDS == 604800
    assert SEAT_LIVE_SECONDS == 600


@pytest.mark.parametrize(
    "age,reason",
    [
        (60.0, "live-holder"),
        (120.0, "live-holder"),    # the owner's 2-minute restart
        (599.0, "live-holder"),
        (601.0, "grace-window"),
        (86400.0, "grace-window"),
    ],
)
def test_without_a_certificate_every_rung_still_blocks(age, reason):
    """Unchanged behaviour when nobody said goodbye. Pins WHICH rung, so a
    future edit cannot fix this for the wrong reason and read as success."""
    d = allocation_decision(
        root=False, lane=False, age=age, holds_mail=False,
        presence_fresh=False, live_at_address=False, certified_dead=False,
    )
    assert d["would_skip"] is True
    assert d["reason"] == reason


@pytest.mark.parametrize("age", [60.0, 120.0, 599.0, 601.0, 86400.0])
def test_a_certificate_frees_the_chair_at_every_age(age):
    """SEAT-13. Testimony outranks a heartbeat at every rung, which is this
    codebase's own rule applied where it had never been applied."""
    d = allocation_decision(
        root=False, lane=False, age=age, holds_mail=False,
        presence_fresh=False, live_at_address=False, certified_dead=True,
    )
    assert d["would_skip"] is False, f"certified dead at age={age} must be free"


def test_a_certificate_never_frees_a_chair_holding_mail():
    """THE SAFETY RUNG. A corpse's inbox is never handed to a successor."""
    d = allocation_decision(
        root=False, lane=False, age=120.0, holds_mail=True,
        presence_fresh=False, live_at_address=False, certified_dead=True,
    )
    assert d["would_skip"] is True


def test_a_certificate_never_overrides_a_reserved_name():
    """Reservation outranks testimony: a lane is not an occupant."""
    for root, lane in ((True, False), (False, True)):
        d = allocation_decision(
            root=root, lane=lane, age=120.0, holds_mail=False,
            presence_fresh=False, live_at_address=False, certified_dead=True,
        )
        assert d["would_skip"] is True


# ── the owner's symptom, end to end ─────────────────────────────────────────

async def test_a_session_that_said_goodbye_gets_its_own_name_back(services):
    """THE CASE HE REPORTED. Claim, exit cleanly, come back — same name.

    Before SEAT-13 this returned `<base>-3`, because the chair it had just
    vacated still read `live-holder` for ten minutes.
    """
    await _cleanup()
    try:
        first = await seat_claim(
            session_key="seat13-a", project=PROJECT, provider="claude",
            host="testhost", session_nonce="nonce-a",
        )
        seat = first["seat"]

        await death_certify(
            session_key="seat13-a", seat=seat, lane=None, project=PROJECT,
            provider="claude", host="testhost",
            died_at=datetime.now(timezone.utc),
            cause="exit", graceful=True, certified_by="test",
        )

        second = await seat_claim(
            session_key="seat13-b", project=PROJECT, provider="claude",
            host="testhost", session_nonce="nonce-b",
        )
        assert second["seat"] == seat, (
            f"a returning session should get {seat!r} back after its "
            f"predecessor certified its own death; got {second['seat']!r}"
        )
    finally:
        await _cleanup()


async def test_a_session_that_merely_went_quiet_keeps_its_name(services):
    """THE OWNER'S CONSTRAINT, stated before he stepped away: do not break
    sessions that are active but currently idle.

    No certificate, no goodbye — just silence. The chair must stay held, and
    a newcomer must be pushed to a different name exactly as before.
    """
    await _cleanup()
    try:
        first = await seat_claim(
            session_key="seat13-quiet", project=PROJECT, provider="claude",
            host="testhost", session_nonce="nonce-q",
        )
        second = await seat_claim(
            session_key="seat13-newcomer", project=PROJECT, provider="claude",
            host="testhost", session_nonce="nonce-n",
        )
        assert second["seat"] != first["seat"], (
            "an idle session with no certificate must keep its chair — this "
            "is the property that protects a team stalled on an allocation "
            "reset"
        )
    finally:
        await _cleanup()
