"""SEAT-13b: a death certificate must name the OCCUPANT, not just the chair.

WHAT THIS COST, 2026-09-22, an hour after SEAT-13 shipped. I ran a throwaway
bridge to verify the clean-exit path. It inherited its parent's environment,
resolved to that parent's seat, and on exit filed a death certificate naming a
LIVE session — mine. With SEAT-13's rung live, my own chair was free for the
taking while I was sitting in it.

WHY session_key CANNOT CATCH THIS. The key is STABLE across respawns by design
(EXIT-NOTICE-2): it names a chair, never an occupant. Two processes can hold
the same key at the same time and the store cannot tell them apart by it. The
process nonce can.

WHY THE EXISTING GUARD DID NOT CATCH IT EITHER. `_cert_is_for_this_occupant`
required the cert to POSTDATE the seat row — which correctly rejects a
PREDECESSOR's late certificate. A cert about a session that is still sitting
here postdates the row by construction. The guard was aimed at exactly the
wrong direction of the same mistake.

⚠️ READ `test_the_incident_itself_is_NOT_yet_closed` BEFORE TRUSTING THIS FIX.
The nonce match closes foreign and late certificates. It does NOT close the
specific incident above, because the second process's claim OVERWRITES the
row's nonce before it ever files the certificate. That half is still open and
the test below is written to fail the day someone fixes it.
"""

from datetime import datetime, timedelta, timezone

import pytest

from server.services.memory_service import PRESENCE_NAMESPACE, get_pool
from server.services.session_registry import (
    _cert_is_for_this_occupant,
    death_certify,
    seat_claim,
)

PROJECT = "seat13bprobe"


async def _cleanup():
    pool = await get_pool()
    async with pool.acquire() as conn:
        for scope in ("seat", "death", "presence"):
            await conn.execute(
                "DELETE FROM memories WHERE namespace = $1 AND scope = $2 "
                "AND (project = $3 OR user_id = $3)",
                PRESENCE_NAMESPACE, scope, PROJECT,
            )


async def _seat_row(seat):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT metadata, created_at FROM memories "
            "WHERE namespace = $1 AND scope = 'seat' AND project = $2 "
            "AND key = $3",
            PRESENCE_NAMESPACE, PROJECT, f"seat/{seat}",
        )


# ── the guard in isolation ──────────────────────────────────────────────────

class _Row(dict):
    """Minimal stand-in: the guard reads metadata and created_at only."""
    def keys(self):
        return super().keys()
    def __getitem__(self, k):
        return super().__getitem__(k)


def _row(*, certified=True, cert_nonce=None, row_nonce=None, skew=60):
    created = datetime.now(timezone.utc)
    md = {"session_nonce": row_nonce} if row_nonce else {}
    if certified:
        md["death_certified"] = True
        md["death_certified_at"] = (created + timedelta(seconds=skew)).isoformat()
        if cert_nonce:
            md["death_certified_nonce"] = cert_nonce
    return _Row(metadata=md, created_at=created)


def test_a_certificate_from_another_process_never_frees_the_chair():
    """THE FIX. Both sides name an incarnation and they disagree — so the
    certificate is testimony about somebody else, whatever its clock says."""
    assert _cert_is_for_this_occupant(
        _row(cert_nonce="probe-nonce", row_nonce="live-nonce")) is False


def test_the_mismatch_beats_a_perfectly_good_timestamp():
    """The bad certificate's timestamp ALWAYS looks good — it postdates the
    row by construction. So the nonce is checked BEFORE the clock, and no
    amount of skew rescues a foreign cert."""
    for skew in (1, 60, 86400):
        assert _cert_is_for_this_occupant(
            _row(cert_nonce="probe", row_nonce="live", skew=skew)) is False


def test_the_occupants_own_certificate_still_frees_the_chair():
    """SEAT-13 must survive its own fix: the owner's restart still works."""
    assert _cert_is_for_this_occupant(
        _row(cert_nonce="same-nonce", row_nonce="same-nonce")) is True


@pytest.mark.parametrize("cert_nonce,row_nonce", [
    (None, "live-nonce"),   # older bridge files a nonce-less cert
    ("probe", None),        # seat row predates the field
    (None, None),           # both predate it
])
def test_a_missing_nonce_falls_back_to_the_clock(cert_nonce, row_nonce):
    """DELIBERATE, AND A KNOWN NARROWER HOLE. An older bridge cannot send a
    nonce, and refusing its certs outright would break the very restart case
    SEAT-13 exists to fix. The fallback is no worse than what shipped; the
    match is strictly better wherever it applies."""
    assert _cert_is_for_this_occupant(
        _row(cert_nonce=cert_nonce, row_nonce=row_nonce)) is True


def test_no_certificate_is_still_no_certificate():
    assert _cert_is_for_this_occupant(_row(certified=False)) is False
    assert _cert_is_for_this_occupant(None) is False


# ── end to end ──────────────────────────────────────────────────────────────

async def test_the_nonce_travels_from_the_certificate_to_the_seat_row(services):
    """Plumbing: a nonce sent to the intake must land where the guard reads
    it. A guard reading a field nothing writes is a guard that never fires."""
    await _cleanup()
    try:
        first = await seat_claim(
            session_key="s13b-key", project=PROJECT, provider="claude",
            host="testhost", session_nonce="nonce-alive",
        )
        await death_certify(
            session_key="s13b-key", seat=first["seat"], lane=None,
            project=PROJECT, provider="claude", host="testhost",
            died_at=datetime.now(timezone.utc), cause="exit", graceful=True,
            certified_by="test", session_nonce="nonce-of-some-other-process",
        )
        row = await _seat_row(first["seat"])
        md = row["metadata"]
        if isinstance(md, str):
            import json
            md = json.loads(md)
        assert md.get("death_certified_nonce") == "nonce-of-some-other-process"
        assert md.get("session_nonce") == "nonce-alive"
        assert _cert_is_for_this_occupant(row) is False, (
            "a certificate naming a different process must not free a live "
            "session's chair"
        )
    finally:
        await _cleanup()


@pytest.mark.xfail(
    strict=True,
    reason="SEAT-13b RESIDUAL, open and pinned. The second process CLAIMS "
           "before it certifies, and a same-key claim rewrites the seat row's "
           "session_nonce (session_registry: the _meta() refresh on the held "
           "row). By the time the certificate is filed the row already names "
           "the intruder, so the nonces match and the guard passes it. "
           "Closing this needs the CLAIM path to refuse to silently displace "
           "a live occupant sharing its key — not another rule at the "
           "certificate. This test is written to FAIL the day that lands.",
)
async def test_the_incident_itself_is_NOT_yet_closed(services):
    """THE INCIDENT, REPRODUCED HONESTLY.

    A live session holds a chair. A second process inherits its environment,
    resolves the same session_key, claims, and exits — certifying a session
    that is still running.

    This test asserts what SHOULD be true. It currently fails, and that is the
    point: the fix shipped alongside it does not reach this case, and a green
    suite must not be allowed to imply otherwise.
    """
    await _cleanup()
    try:
        live = await seat_claim(
            session_key="s13b-shared", project=PROJECT, provider="claude",
            host="testhost", session_nonce="nonce-live",
        )
        # The probe: same key, its own process identity.
        await seat_claim(
            session_key="s13b-shared", project=PROJECT, provider="claude",
            host="testhost", session_nonce="nonce-probe",
        )
        await death_certify(
            session_key="s13b-shared", seat=live["seat"], lane=None,
            project=PROJECT, provider="claude", host="testhost",
            died_at=datetime.now(timezone.utc), cause="exit", graceful=True,
            certified_by="test", session_nonce="nonce-probe",
        )
        row = await _seat_row(live["seat"])
        assert _cert_is_for_this_occupant(row) is False, (
            "a probe sharing a session key must not be able to certify a "
            "live session dead"
        )
    finally:
        await _cleanup()
