"""SEAT-13: a session's own goodbye does not free its seat. EXPECTED TO FAIL.

THE SYMPTOM THE OWNER REPORTS, in his words (2026-09-22): "I run 8 hrs,
determine context needs refreshing and I exit the session. 2 minutes later I
spawn the same session again, all I want is a new context. Almost 100% of the
time I get a <name>-n, and n is weird, could be 1, or 10."

THE MECHANISM. The bridge files a death certificate on every clean exit
(EXIT-NOTICE-1) and the roster renders it as EXITED. But `allocation_decision`
has no rung for "we were told it died": the ladder is root -> lane -> live ->
grace -> mail -> fresh -> free, and a chair that stopped beating two minutes
ago is inside `grace-window`, which is SEAT_GRACE_SECONDS = 7 DAYS. So the
goodbye is recorded, visible, and irrelevant to the only decision it should
settle, and every restart inside a week takes the next ordinal up.

The registry's own comment names the goodbye as one of three ways an address
comes back -- "covers hand-launched sessions that no orchestrator ever spawned"
-- which is exactly the owner's case. That promise is not kept.

WHY THIS TEST IS COMMITTED RED. Whether a farewell may shorten the backstop is
SEAT-13, and SEAT-13 is the owner's decision, stated in DeathCertRequest's own
docstring: "It does NOT free the seat or accelerate reclamation -- that question
is SEAT-13, the owner's, deliberately untouched." Two shapes are on the ledger
for him to choose between. Shipping either without him is how the 2026-08-22
change got made, and that one evicted a live session on 2026-09-22.

So this is the artefact that has been missing from every previous round of this
conversation: a failing test that reproduces the owner's symptom, written
BEFORE the fix, so that whichever shape he picks can be judged by whether this
goes green rather than by anyone's confidence.

RUN:  pytest tests/test_seat13_goodbye_does_not_free.py -v
TODAY: xfail (the defect is present).
AFTER SEAT-13 SHIPS: it should XPASS -- remove the marker and keep the test.
"""

import pytest

from server.services.session_registry import (
    SEAT_GRACE_SECONDS,
    SEAT_LIVE_SECONDS,
    allocation_decision,
)


def test_grace_window_is_seven_days():
    """Anchors the number the owner is waiting out. Not a defect, context."""
    assert SEAT_GRACE_SECONDS == 604800, "7d backstop"
    assert SEAT_LIVE_SECONDS == 600


@pytest.mark.xfail(
    reason="SEAT-13 is the owner's decision and is not implemented: a "
           "certified goodbye does not free the seat, so a clean exit still "
           "waits out the 7d backstop and the next session takes an ordinal.",
    strict=True,
)
def test_a_chair_whose_session_said_goodbye_is_free():
    """THE OWNER'S CASE. Exited cleanly, certified dead, back 2 minutes later.

    `age` is 120s: older than nothing, far inside the 7-day window. Every
    other fact is benign -- not reserved, no mail parked, nothing breathing
    at the address. The ONLY thing standing between this session and its own
    name is that the ladder cannot hear a goodbye.

    When SEAT-13 lands, `allocation_decision` gains a fact for certified
    death and this stops skipping. Until then it parks on `grace-window`.
    """
    decision = allocation_decision(
        root=False,
        lane=False,
        age=120.0,            # exited 2 minutes ago
        holds_mail=False,     # inbox drained, as the wrapup rules instruct
        presence_fresh=False, # nothing beating
        live_at_address=False,
    )
    assert decision["would_skip"] is False, (
        f"a certified-dead chair should be free; got "
        f"{decision['reason']!r} (grace expires "
        f"{decision.get('grace_expires_at')})"
    )


@pytest.mark.parametrize(
    "age,reason",
    [
        (60.0, "live-holder"),     # 1 min after a clean exit
        (120.0, "live-holder"),    # the owner's 2-minute restart
        (599.0, "live-holder"),    # last second before the handover
        (601.0, "grace-window"),   # and from here, six more days of it
        (86400.0, "grace-window"),
    ],
)
def test_which_rung_actually_blocks_a_returning_session(age, reason):
    """Pins WHY it skips at each age, measured rather than assumed.

    ⚠️ THIS TEST CORRECTED ITS AUTHOR ON ITS FIRST RUN. I told the owner the
    seven-day backstop was what blocked his two-minute restart. It is not: at
    120s the ladder answers `live-holder`, because `age < SEAT_LIVE_SECONDS`
    (600s) and the row still looks alive. The 7-day window is real and blocks
    every LATER attempt; the immediate one is refused for a different reason.

    Both rungs read the same `age`, and `age` is time since `last_used_at` --
    the field a WATCHER refreshes (LAST-SPOKE-IS-THE-WATCHER-1). So a session
    that exits cleanly can look "live" for ten minutes on the strength of its
    own watcher's dying beats, and then "recently departed" for a week.

    Whichever shape SEAT-13 takes, it has to clear BOTH rungs, and the first
    one is not a time window at all -- it is the store believing the corpse is
    still working.
    """
    decision = allocation_decision(
        root=False, lane=False, age=age,
        holds_mail=False, presence_fresh=False, live_at_address=False,
    )
    assert decision["would_skip"] is True
    assert decision["reason"] == reason
