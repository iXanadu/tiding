"""Seat registry — allocating session addresses instead of computing them.

Every session used to COMPUTE its own inbox address from data that is not
unique (the project folder, optionally suffixed with the provider). Three Claude
sessions in one folder therefore collapsed onto one identity: shared ack-state,
mutual self-echo drop, unable to wake each other. ``docs/messaging.md`` has
always stated the invariant — "two agents never share an identity; they share
subscriptions" — but nothing enforced it; collisions were detected after the
fact and handed to a human to fix.

This module makes the invariant true by construction: a session CLAIMS a seat
and the server hands back one nobody else holds.

Why the server and not the launcher: a launcher can only dedupe the sessions it
launched. A hand-started terminal session is invisible to it, so two launchers —
or a launcher and a human — can hand out the same seat. engram is the only party
that sees every session regardless of who spawned it.

Rows reuse the memories table exactly as inbox and presence do — zero schema
migration:

    namespace = <primary>          (SEAT_NAMESPACE)
    scope     = 'seat'
    user_id   = 'global'
    project   = <bare project name>
    key       = 'seat/<seat>'      — one row per allocated address
    metadata  = {session_key, session_nonce, provider, host, ...}

``last_used_at`` is the claim heartbeat. Seat rows carry NO embedding (the
column is nullable and a seat is never semantically searched), so a claim is a
pure registry write — cheap enough to refresh on the existing presence beat.

See docs/design/session-registry.md for the full design, including the four
holes this implementation closes.
"""

import json
from datetime import datetime, timedelta, timezone

from server.db import get_pool
from server.services.memory_service import (
    INBOX_NAMESPACE,
    INBOX_OPEN,
    INBOX_SCOPE,
    PRESENCE_SCOPE,
    SEAT_EXEMPT_IDENTITIES,
    SEAT_SCOPE,
    SEAT_USER_ID,
)

SEAT_NAMESPACE = INBOX_NAMESPACE

# A seat whose holder beat within this window is LIVE — never reassigned, and
# the window that distinguishes a duplicate session_key from a genuine restart.
# Matches the presence staleness threshold so "stale on the roster" and "no
# longer live in the registry" mean the same thing to a reader.
SEAT_LIVE_SECONDS = 600

# Past this, a seat is RECLAIMABLE.
#
# Deliberately very long, because RECLAMATION BUYS ALMOST NOTHING AND RISKS A
# LOT. A session restarting with a stable session_key gets its own seat back
# through the continuity check above, which has no age condition at all — so
# reclamation serves exactly one purpose: letting a genuinely NEW session reuse
# an abandoned ordinal to keep numbering tight. That is cosmetic.
#
# What it risks is not cosmetic. Liveness is inferred from heartbeats, and
# heartbeats only fire on tool calls, so a session doing long uninterrupted
# work looks identical to a dead one — and it is precisely the session you
# least want to disturb. Observed 2026-07-24: a live session, quiet 4.8h while
# running its own build, with its seat reading reclaimable while it was still
# listening. Reclaiming it would have handed its address to a newcomer.
#
# So the window is set past any plausible quiet stretch rather than past a
# plausible pause. The real fix is a liveness signal that does not depend on
# the agent speaking (the watcher, which polls on its own timer — SEAT-7);
# until that exists, this is the conservative trade: slightly untidier
# ordinals, no stolen addresses.
SEAT_GRACE_SECONDS = 86400  # 24h

# Refuse rather than allocate unbounded. A project needing >64 concurrent
# sessions is a misconfiguration (usually a session_key that changes every
# call), and silently minting seat -517 would hide it.
MAX_SEAT_ORDINAL = 64


def seat_candidates(project: str, provider: str, preferred: str | None = None):
    """Yield candidate seats, lowest ordinal first (low-water-mark allocation).

    Lowest-first matters: after churn, numbering stays tight instead of drifting
    upward forever, so ``proj-claude-2`` means "the second live session" rather
    than "the 47th session this project ever had".
    """
    base = f"{project}-{provider}"
    if preferred and preferred != base:
        yield preferred
    yield base
    for n in range(2, MAX_SEAT_ORDINAL + 1):
        yield f"{base}-{n}"


def _md(row) -> dict:
    md = row["metadata"] if row else None
    if isinstance(md, str):
        return json.loads(md)
    return md or {}


async def _has_undelivered_mail(conn, seat: str) -> bool:
    """Is there open, never-read mail addressed to this seat?

    The hard guard on reclamation. Mail in flight for a session that died must
    be preserved for whoever next holds the address, never handed to a stranger
    who happens to be allocated the same ordinal. Correctness (R8) outranks
    tidy numbering (R7), so an undelivered message parks the seat indefinitely.
    """
    row = await conn.fetchrow(
        """
        SELECT 1 FROM memories
        WHERE namespace = $1 AND scope = $2 AND user_id = $3
          AND COALESCE(metadata->>'status', $4) = $4
          AND COALESCE(jsonb_array_length(metadata->'read_by'), 0) = 0
        LIMIT 1
        """,
        INBOX_NAMESPACE, INBOX_SCOPE, seat, INBOX_OPEN,
    )
    return row is not None


async def _presence_is_fresh(conn, seat: str, project: str) -> bool:
    """Is a session independently heartbeating at this address right now?

    The seat row and the presence row are two clocks on the same session, and
    they disagreed in production: the roster reported a session fresh at 374
    seconds while its seat read not-live and reclaimable, because the seat's
    timestamp was written once at claim time and never refreshed. Reclaiming
    on the seat clock alone would therefore hand a running session's address
    to a newcomer — the collision seats exist to prevent, arriving through
    reclamation instead of allocation.

    So takeover consults BOTH: presence is the signal that the holder is
    actually alive, and a fresh one vetoes reclamation outright. Defence in
    depth alongside the heartbeat now refreshing the seat directly — that fix
    keeps the clocks together, this one is what holds if they ever drift
    again.
    """
    row = await conn.fetchrow(
        """
        SELECT last_used_at FROM memories
        WHERE namespace = $1 AND scope = $2 AND user_id = $3 AND key = $4
        """,
        SEAT_NAMESPACE, PRESENCE_SCOPE, project, f"presence/{seat}",
    )
    if row is None:
        return False
    age = (datetime.now(timezone.utc) - row["last_used_at"]).total_seconds()
    return age < SEAT_LIVE_SECONDS


async def _try_insert(conn, seat: str, project: str, meta: dict) -> bool:
    """Atomically claim a free seat. True if we got it.

    ``ON CONFLICT DO NOTHING`` against the existing
    ``UNIQUE NULLS NOT DISTINCT (namespace, key, scope, user_id, project)``
    is a correct compare-and-swap: concurrent claimants are serialised by the
    index, exactly one inserts, the losers get zero rows and advance to the next
    ordinal. No advisory locks, no retry loop, no transaction gymnastics.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO memories
            (namespace, key, value, scope, user_id, project,
             tags, tags_search, search_text, embedding, metadata)
        VALUES ($1, $2, $3, $4, $5, $6, '', '', $3, NULL, $7::jsonb)
        ON CONFLICT (namespace, key, scope, user_id, project) DO NOTHING
        RETURNING key
        """,
        SEAT_NAMESPACE, f"seat/{seat}", seat, SEAT_SCOPE, SEAT_USER_ID,
        project, json.dumps(meta),
    )
    return row is not None


async def _try_takeover(conn, seat: str, project: str, meta: dict,
                        older_than: datetime) -> bool:
    """Take over an abandoned seat. True if we got it.

    The ``last_used_at`` guard in the WHERE clause is what makes this safe under
    concurrency: if the previous holder heartbeats between our read and our
    write, the UPDATE matches zero rows and we move on rather than evicting a
    live session.
    """
    row = await conn.fetchrow(
        """
        UPDATE memories
        SET metadata = $1::jsonb, last_used_at = NOW()
        WHERE namespace = $2 AND scope = $3 AND user_id = $4 AND project = $5
          AND key = $6 AND last_used_at < $7
        RETURNING key
        """,
        json.dumps(meta), SEAT_NAMESPACE, SEAT_SCOPE, SEAT_USER_ID,
        project, f"seat/{seat}", older_than,
    )
    return row is not None


def _meta(session_key: str, session_nonce: str | None, provider: str,
          host: str | None) -> dict:
    return {
        "kind": "seat",
        "session_key": session_key,
        "session_nonce": session_nonce,
        "provider": provider,
        "host": host,
        "claimed_at": datetime.now(timezone.utc).isoformat(),
    }


async def seat_claim(
    session_key: str,
    project: str,
    provider: str,
    session_nonce: str | None = None,
    host: str | None = None,
    preferred_seat: str | None = None,
) -> dict:
    """Allocate (or re-confirm) this session's unique inbox address.

    Idempotent on ``session_key``: a bridge restart or a harness respawn
    re-claims the SAME seat instead of burning an ordinal, so a session's
    address never changes underneath it or its watcher.

    Returns ``{seat, is_new, reclaimed_from, warning}``.
    """
    project = (project or "").strip().lower()
    provider = (provider or "claude").strip().lower()
    preferred = (preferred_seat or "").strip().lower() or None

    # Deliberate role-sharing stays shared. ``admin`` is one identity worn by
    # maintenance sessions on several boxes on purpose; allocating them apart
    # would "fix" something that is not broken.
    if preferred in SEAT_EXEMPT_IDENTITIES or project in SEAT_EXEMPT_IDENTITIES:
        return {
            "seat": preferred or project,
            "is_new": False,
            "reclaimed_from": None,
            "warning": None,
        }

    now = datetime.now(timezone.utc)
    live_cutoff = now - timedelta(seconds=SEAT_LIVE_SECONDS)
    grace_cutoff = now - timedelta(seconds=SEAT_GRACE_SECONDS)
    meta = _meta(session_key, session_nonce, provider, host)

    pool = await get_pool()
    async with pool.acquire() as conn:
        # 1. Continuity: do I already hold a seat in this project?
        #
        # session_key means CONTINUITY, (key, nonce) means IDENTITY. A claim
        # bearing a known key but a different nonce while the holder is still
        # LIVE is not a restart — it is two processes sharing one key, which is
        # the very collision this registry exists to prevent. Blessing it would
        # reintroduce the bug with the server's endorsement, so that case falls
        # through to allocation and is reported.
        held = await conn.fetchrow(
            """
            SELECT key, metadata, last_used_at FROM memories
            WHERE namespace = $1 AND scope = $2 AND user_id = $3 AND project = $4
              AND metadata->>'session_key' = $5
            """,
            SEAT_NAMESPACE, SEAT_SCOPE, SEAT_USER_ID, project, session_key,
        )
        warning = None
        if held:
            held_md = _md(held)
            held_nonce = held_md.get("session_nonce")
            same_process = (
                not session_nonce or not held_nonce or held_nonce == session_nonce
            )
            if same_process or held["last_used_at"] < live_cutoff:
                # Same process, or the previous holder is no longer live: a
                # genuine restart of the same logical session. Keep the seat.
                seat = held["key"].removeprefix("seat/")
                await conn.execute(
                    """
                    UPDATE memories SET metadata = $1::jsonb, last_used_at = NOW()
                    WHERE namespace = $2 AND scope = $3 AND user_id = $4
                      AND project = $5 AND key = $6
                    """,
                    json.dumps(meta), SEAT_NAMESPACE, SEAT_SCOPE,
                    SEAT_USER_ID, project, held["key"],
                )
                return {"seat": seat, "is_new": False,
                        "reclaimed_from": None, "warning": None}
            warning = (
                f"session_key {session_key!r} is already held by a LIVE session "
                f"with a different process nonce — it is not unique. Allocating a "
                f"separate seat so you are still individually addressable, but "
                f"whatever generates this key must be fixed."
            )

        # 2-5. Allocate: first free candidate, then first reclaimable one.
        for seat in seat_candidates(project, provider, preferred):
            if await _try_insert(conn, seat, project, meta):
                return {"seat": seat, "is_new": True,
                        "reclaimed_from": None, "warning": warning}

            row = await conn.fetchrow(
                """
                SELECT metadata, last_used_at FROM memories
                WHERE namespace = $1 AND scope = $2 AND user_id = $3
                  AND project = $4 AND key = $5
                """,
                SEAT_NAMESPACE, SEAT_SCOPE, SEAT_USER_ID, project, f"seat/{seat}",
            )
            if row is None:
                continue  # freed between insert and read — next loop retries it
            if row["last_used_at"] >= live_cutoff:
                continue  # live holder; never evict

            prior = _md(row)
            # ONE condition for takeover: the seat is past the full grace window.
            #
            # There used to be a "same slot" shortcut here — same provider, same
            # host, past the LIVE window — meant to let a harness that restarted
            # with a new session_key re-take its own seat instead of drifting to
            # an ordinal. It permitted a takeover after only ~10 minutes of
            # quiet, and it could not tell "my own restart" from "a different
            # session that happens to match provider and host". With liveness
            # inferred from tool activity, ten minutes of quiet is an ordinary
            # state for a working session, so the shortcut was a live-address
            # steal waiting for the right timing.
            #
            # Dropping it costs only tidiness, and only in the narrow case of a
            # session whose KEY changed (a hand-launched restart): it gets a
            # fresh ordinal rather than its old one. Launcher-spawned sessions
            # are unaffected — their key is stable, so continuity returns their
            # seat directly.
            if row["last_used_at"] >= grace_cutoff:
                continue
            if await _has_undelivered_mail(conn, seat):
                continue  # R8: never hand a stranger someone else's mail
            if await _presence_is_fresh(conn, seat, project):
                continue  # SEAT-8: an independently-live session holds this

            if await _try_takeover(conn, seat, project, meta,
                                   older_than=live_cutoff):
                return {
                    "seat": seat,
                    "is_new": True,
                    "reclaimed_from": prior.get("session_key"),
                    "warning": warning,
                }

    raise ValueError(
        f"no free seat for project {project!r} provider {provider!r} after "
        f"{MAX_SEAT_ORDINAL} candidates — this almost always means the caller's "
        f"session_key changes on every claim rather than that {MAX_SEAT_ORDINAL} "
        f"sessions are genuinely live."
    )


async def seat_release(session_key: str, project: str) -> str | None:
    """Release this session's seat. Returns the freed seat, or None.

    The clean path, always preferable to waiting out the grace period: an
    explicit release returns the ordinal immediately so the next session gets a
    tight number instead of the next one up.
    """
    project = (project or "").strip().lower()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            DELETE FROM memories
            WHERE namespace = $1 AND scope = $2 AND user_id = $3 AND project = $4
              AND metadata->>'session_key' = $5
            RETURNING key
            """,
            SEAT_NAMESPACE, SEAT_SCOPE, SEAT_USER_ID, project, session_key,
        )
    return row["key"].removeprefix("seat/") if row else None


# NOTE (2026-07-24, Rob): no role-as-address. A role is not unique and not
# provider-stable — "engram-tester" for grok and "engram-tester" for claude
# collide, which is the exact two-bodies-one-identity bug seats exist to kill.
# Roles are assigned at HUDDLE time to whichever seats the owner picked, and
# live in the orchestration layer (AgentBeast), never in an engram address.
# Addressing is two layers only: the project GROUP and the unique provider-
# discriminated SEAT. A seat_alias() lived here briefly; it was the mistake.


async def seat_list(
    project: str | None = None,
    session_key: str | None = None,
) -> list[dict]:
    """Allocated seats, freshest first. The registry's read side.

    ``session_key`` answers "what address does the session I spawned actually
    hold?" — the LAUNCHER's question. A launcher never calls /session/claim
    (the bridge inside the session does), so the claim response cannot reach
    it; the key it generated is the only join it has, and it is the one thing
    it is certain of.

    That matters because a launcher that reconstructs the seat locally is
    guessing: the server may grant an ordinal when a peer already holds the
    preferred address, and the guess then misses SILENTLY. Reading the granted
    seat is the difference between knowing and inferring.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT key, project, metadata, last_used_at FROM memories
            WHERE namespace = $1 AND scope = $2
              AND ($3::text IS NULL OR project = $3)
              AND ($4::text IS NULL OR metadata->>'session_key' = $4)
            ORDER BY last_used_at DESC
            """,
            SEAT_NAMESPACE, SEAT_SCOPE,
            project.strip().lower() if project else None,
            session_key.strip().lower() if session_key else None,
        )
    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        md = _md(r)
        age = (now - r["last_used_at"]).total_seconds()
        out.append({
            "seat": r["key"].removeprefix("seat/"),
            "project": r["project"],
            "provider": md.get("provider"),
            "host": md.get("host"),
            "session_key": md.get("session_key"),
            "age_seconds": round(age, 1),
            "is_live": age < SEAT_LIVE_SECONDS,
            "reclaimable": age >= SEAT_GRACE_SECONDS,
        })
    return out
