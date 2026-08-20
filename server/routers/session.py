"""Seat registry endpoints — sessions claim addresses, they don't compute them.

The registry is deliberately a separate surface from ``/memory/presence``:
presence answers "is anyone home at this address", the registry answers "whose
address is this". Conflating them is what let two sessions upsert one presence
row and silently share an identity.

See docs/design/session-registry.md.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from server.dependencies import check_namespace_access, get_current_principal
from server.models import (
    AddressEntry,
    AddressRegisterResponse,
    DeathCertRequest,
    DeathCertResponse,
    ProjectRegistryEntry,
    ProjectRegistryResponse,
    SeatClaimRequest,
    SeatClaimResponse,
    SeatEntry,
    SeatListRequest,
    SeatListResponse,
    SeatReleaseRequest,
    SeatReleaseResponse,
)
from server.services.session_registry import (
    SEAT_NAMESPACE,
    address_register,
    death_certify,
    is_reserved_lane,
    project_registry,
    seat_claim,
    seat_list,
    seat_release,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/session", tags=["session"])


@router.post("/claim", response_model=SeatClaimResponse)
async def claim_seat(req: SeatClaimRequest, request: Request):
    """Allocate (or re-confirm) this session's unique inbox address.

    Idempotent on ``session_key`` — a bridge restart re-claims the same seat
    rather than burning an ordinal, so a running session's address never moves
    underneath it or its watcher.
    """
    check_namespace_access(get_current_principal(request), SEAT_NAMESPACE, "write")
    try:
        result = await seat_claim(
            session_key=req.session_key,
            project=req.project,
            provider=req.provider,
            session_nonce=req.session_nonce,
            host=req.host,
            preferred_seat=req.preferred_seat,
            runtime_seat=req.runtime_seat,
        )
    except ValueError as e:
        # Exhausted ordinals is a caller problem (a session_key that changes
        # every claim), not a server fault — 409 so it is actionable.
        raise HTTPException(status_code=409, detail=str(e))
    except Exception:
        logger.exception("seat_claim failed")
        raise HTTPException(status_code=500, detail="internal error — see server logs")

    seat = result["seat"]
    guidance = (
        f"You hold seat '{seat}'. Listen on it AND on your project group "
        f"address '{req.project}' (group mail still reaches you). "
        f"Your watcher MUST resolve the same seat — if it was armed before "
        f"this claim it will pick the seat up from the seat file on its next "
        f"poll. This seat is your whole address; roles (tester, orchestrator) "
        f"are assigned in the huddle, not encoded here."
    )
    if result.get("renamed_from"):
        guidance = (
            f"Registration MOVED from '{result['renamed_from']}' to '{seat}' "
            f"(runtime seat). Continuity now returns '{seat}'.\n\n" + guidance
        )
    if result.get("warning"):
        guidance = f"⛔ {result['warning']}\n\n{guidance}"
    return SeatClaimResponse(
        status="ok",
        seat=seat,
        is_new=result.get("is_new", False),
        reclaimed_from=result.get("reclaimed_from"),
        warning=result.get("warning"),
        renamed_from=result.get("renamed_from"),
        guidance=guidance,
    )


@router.post("/release", response_model=SeatReleaseResponse)
async def release_seat(req: SeatReleaseRequest, request: Request):
    """Free this session's seat immediately.

    The clean path: an explicit release returns the ordinal now, so the next
    session gets a tight number instead of waiting out the grace period.
    """
    check_namespace_access(get_current_principal(request), SEAT_NAMESPACE, "write")
    try:
        released = await seat_release(req.session_key, req.project)
    except Exception:
        logger.exception("seat_release failed")
        raise HTTPException(status_code=500, detail="internal error — see server logs")
    return SeatReleaseResponse(status="ok", released=released)


@router.post("/death", response_model=DeathCertResponse)
async def certify_death(req: DeathCertRequest, request: Request):
    """LANE-4: a spawner certifies an occupant's death (testimony intake).

    Accepted even while the presence row looks live — a heartbeat can
    outlive a kill, never observe one. Rejections are for MALFORMED certs
    only: both idempotency keys absent, or (reservation on) a seat that is
    the lane string — the spawner-side seat_for() fallback trap, which
    would certify that the LANE died. Pre-reservation that same equality is
    an honest first occupant and passes (PM amendment, 2026-08-14).

    Not admin-gated, and no stricter than the rest of /session/* (PM ack):
    the middleware's posture decides who gets here at all. A certificate is
    testimony and testimony carries a name — so the certifier is RECORDED
    (principal name when authenticated, else the middleware's auth_source,
    e.g. legacy/loopback), never silently absent.
    """
    principal = get_current_principal(request)
    # The principal is a dict from the middleware, not an object — the first
    # live cert (2026-08-14, death/claude-ab-macmini) recorded a whole dict
    # repr in certified_by because getattr() silently missed. Name only.
    if isinstance(principal, dict):
        certifier = principal.get("name")
    else:
        certifier = getattr(principal, "name", None)
    certifier = (
        certifier
        or (str(principal) if principal else None)
        or getattr(request.state, "auth_source", "anonymous")
    )
    if not req.session_key and not req.seat:
        raise HTTPException(
            status_code=422,
            detail="no idempotency key: session_key is empty and seat is "
                   "empty — send at least one (SEAT-6 fallback is "
                   "seat+died_at)",
        )
    if req.seat and is_reserved_lane(req.seat, req.project):
        raise HTTPException(
            status_code=422,
            detail=f"lane_as_seat: {req.seat!r} is this project's lane, not "
                   f"an occupant — a spawner-side seat_for() fallback filled "
                   f"in the mailbox where the granted seat belongs. Send the "
                   f"granted occupant seat or empty.",
        )
    try:
        result = await death_certify(
            session_key=req.session_key,
            seat=req.seat,
            lane=req.lane,
            project=req.project,
            provider=req.provider,
            host=req.host,
            died_at=req.died_at,
            cause=req.cause,
            graceful=req.graceful,
            certified_by=certifier,
        )
    except Exception:
        logger.exception("death_certify failed")
        raise HTTPException(status_code=500, detail="death intake failed")
    return DeathCertResponse(status="ok", **result)


@router.get("/addresses", response_model=AddressRegisterResponse)
async def list_addresses(request: Request, project: str | None = None):
    """ADDR-REG: every name the store is holding, and why — the owner's view.

    The register /session/seats is not: seats serves allocated rows; this
    additionally explains each name (undrained mail count, death evidence,
    the allocator's own skip reason) and synthesizes names that have NO seat
    row but are parked by open mail (the R8 class invisible everywhere else).
    Fleet-wide by default; ``?project=`` narrows. Facts plus the allocator's
    own policy — see AddressEntry for the contract's honesty rules.
    """
    check_namespace_access(get_current_principal(request), SEAT_NAMESPACE, "read")
    try:
        entries = await address_register(project)
    except Exception:
        logger.exception("address_register failed")
        raise HTTPException(status_code=500, detail="internal error — see server logs")
    return AddressRegisterResponse(
        status="ok",
        generated_at=datetime.now(timezone.utc).isoformat(),
        entries=[AddressEntry(**e) for e in entries],
    )


@router.get("/projects", response_model=ProjectRegistryResponse)
async def list_projects(request: Request):
    """Step 8: every project root the store knows — registered (claim-path
    census) plus observed-only (seat rows predating the registry). The
    address tree's verifiable root: what a sender checks a destination
    against, and where dormant projects stay visible."""
    check_namespace_access(get_current_principal(request), SEAT_NAMESPACE, "read")
    try:
        projects = await project_registry()
    except Exception:
        logger.exception("project_registry failed")
        raise HTTPException(status_code=500, detail="internal error — see server logs")
    return ProjectRegistryResponse(
        status="ok",
        generated_at=datetime.now(timezone.utc).isoformat(),
        projects=[ProjectRegistryEntry(**p) for p in projects],
    )


@router.post("/seats", response_model=SeatListResponse)
async def list_seats(req: SeatListRequest, request: Request):
    """Who holds which address. The registry's read side.

    Pass ``session_key`` for a launcher's direct lookup — "what address did the
    session I spawned actually get?" — so a launcher reads the granted seat
    instead of reconstructing it and silently missing when an ordinal was
    granted. The seat is the whole address: there are no role aliases (a role
    is not unique or provider-stable, so it is never an address — it lives in
    the huddle/orchestration layer).
    """
    check_namespace_access(get_current_principal(request), SEAT_NAMESPACE, "read")
    try:
        seats = await seat_list(req.project, req.session_key)
    except Exception:
        logger.exception("seat_list failed")
        raise HTTPException(status_code=500, detail="internal error — see server logs")
    return SeatListResponse(status="ok", seats=[SeatEntry(**s) for s in seats])


# ─── Watch-claim: one seat, one watch (docs/design/watch-claim.md v2) ────────
# Sensing only. These endpoints never gate delivery (a mute holder must not
# lock out a working deliverer — review kill K2), and a watcher that cannot
# reach them runs UNCLAIMED and loudly UNHELD (kill K3) — the repair crew
# hears each other while the store is sick.

from pydantic import BaseModel as _BM, Field as _F  # local: models.py additions ride next pass

from server.services.watch_claim import (  # noqa: E402
    watch_beat as _watch_beat,
    watch_claim as _watch_claim,
    watch_release as _watch_release,
    watch_status as _watch_status,
)


class WatchClaimRequest(_BM):
    seat: str = _F(max_length=200)
    nonce: str = _F(min_length=8, max_length=64)
    armed_by: str = _F(default="agent", max_length=20)
    project_dir: str = _F(default="", max_length=4096)
    listen_set: list[str] = _F(default_factory=list, max_length=32)
    host: str | None = _F(default=None, max_length=200)


class WatchBeatRequest(_BM):
    seat: str = _F(max_length=200)
    nonce: str = _F(min_length=8, max_length=64)


@router.post("/watch/claim")
async def watch_claim_endpoint(req: WatchClaimRequest, request: Request):
    check_namespace_access(get_current_principal(request), SEAT_NAMESPACE, "write")
    try:
        return await _watch_claim(
            seat=req.seat, nonce=req.nonce, armed_by=req.armed_by,
            project_dir=req.project_dir, listen_set=req.listen_set,
            host=req.host,
        )
    except Exception:
        logger.exception("watch_claim failed")
        raise HTTPException(status_code=500, detail="internal error — see server logs")


@router.post("/watch/beat")
async def watch_beat_endpoint(req: WatchBeatRequest, request: Request):
    check_namespace_access(get_current_principal(request), SEAT_NAMESPACE, "write")
    try:
        return await _watch_beat(seat=req.seat, nonce=req.nonce)
    except Exception:
        logger.exception("watch_beat failed")
        raise HTTPException(status_code=500, detail="internal error — see server logs")


@router.post("/watch/release")
async def watch_release_endpoint(req: WatchBeatRequest, request: Request):
    check_namespace_access(get_current_principal(request), SEAT_NAMESPACE, "write")
    try:
        return await _watch_release(seat=req.seat, nonce=req.nonce)
    except Exception:
        logger.exception("watch_release failed")
        raise HTTPException(status_code=500, detail="internal error — see server logs")


@router.get("/watch/status")
async def watch_status_endpoint(seat: str, request: Request):
    check_namespace_access(get_current_principal(request), SEAT_NAMESPACE, "read")
    try:
        return await _watch_status(seat=seat)
    except Exception:
        logger.exception("watch_status failed")
        raise HTTPException(status_code=500, detail="internal error — see server logs")
