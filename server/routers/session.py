"""Seat registry endpoints — sessions claim addresses, they don't compute them.

The registry is deliberately a separate surface from ``/memory/presence``:
presence answers "is anyone home at this address", the registry answers "whose
address is this". Conflating them is what let two sessions upsert one presence
row and silently share an identity.

See docs/design/session-registry.md.
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from server.dependencies import check_namespace_access, get_current_principal
from server.models import (
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
