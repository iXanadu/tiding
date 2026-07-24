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
    metadata  = {session_key, session_nonce, provider, host, aliases, ...}

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
    SEAT_EXEMPT_IDENTITIES,
)

SEAT_NAMESPACE = INBOX_NAMESPACE
SEAT_SCOPE = "seat"
SEAT_USER_ID = "global"

# A seat whose holder beat within this window is LIVE — never reassigned, and
# the window that distinguishes a duplicate session_key from a genuine restart.
# Matches the presence staleness threshold so "stale on the roster" and "no
# longer live in the registry" mean the same thing to a reader.
SEAT_LIVE_SECONDS = 600

# Past this, a seat is RECLAIMABLE. Deliberately far beyond SEAT_LIVE_SECONDS: a
# closed laptop, a session paused over lunch, or an overnight gap must NOT cost a
# session its address. Between the two windows the seat is QUIET — reclaimable
# only by a session that looks like the same slot (see seat_claim rule 3).
SEAT_GRACE_SECONDS = 7200  # 2h

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
          host: str | None, aliases: list[str] | None = None) -> dict:
    return {
        "kind": "seat",
        "session_key": session_key,
        "session_nonce": session_nonce,
        "provider": provider,
        "host": host,
        "aliases": aliases or [],
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
                meta["aliases"] = held_md.get("aliases") or []
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
            reclaimable = row["last_used_at"] < grace_cutoff
            # QUIET seat, same logical slot: a harness that fully restarted gets
            # a new session_key, so continuity-by-key cannot recognise it. Letting
            # it re-take its own seat is what keeps a restart from drifting to -2
            # while its peers hold -1.
            same_slot = (
                prior.get("provider") == provider
                and prior.get("host") == host
                and host is not None
            )
            if not (reclaimable or same_slot):
                continue
            if await _has_undelivered_mail(conn, seat):
                continue  # R8: never hand a stranger someone else's mail

            meta["aliases"] = []
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


async def seat_alias(session_key: str, project: str, alias: str) -> dict:
    """Bind a ROLE address (``<project>-<alias>``) to this session's seat.

    Additive, never a rename. Two reasons the seat must stay:
      - peers already hold the seat address; renaming invalidates it
      - roles change mid-session ("you take testing now") while the seat must
        stay stable so the session's address never moves under it

    Uniqueness is enforced against the same registry, so two sessions cannot
    both answer to ``-orchestrator``.
    """
    project = (project or "").strip().lower()
    alias = (alias or "").strip().lower()
    if not alias:
        raise ValueError("alias is required")
    full = alias if alias.startswith(f"{project}-") else f"{project}-{alias}"

    pool = await get_pool()
    async with pool.acquire() as conn:
        mine = await conn.fetchrow(
            """
            SELECT key, metadata FROM memories
            WHERE namespace = $1 AND scope = $2 AND user_id = $3 AND project = $4
              AND metadata->>'session_key' = $5
            """,
            SEAT_NAMESPACE, SEAT_SCOPE, SEAT_USER_ID, project, session_key,
        )
        if not mine:
            raise ValueError(
                "no seat held by this session_key — claim a seat before "
                "declaring a role alias"
            )
        taken = await conn.fetchrow(
            """
            SELECT key FROM memories
            WHERE namespace = $1 AND scope = $2 AND user_id = $3 AND project = $4
              AND (key = $5 OR metadata->'aliases' ? $6)
            """,
            SEAT_NAMESPACE, SEAT_SCOPE, SEAT_USER_ID, project,
            f"seat/{full}", full,
        )
        if taken and taken["key"] != mine["key"]:
            raise ValueError(
                f"role address {full!r} is already held by another session in "
                f"this project — ask the roster who has it"
            )
        md = _md(mine)
        aliases = md.get("aliases") or []
        if full not in aliases:
            aliases.append(full)
        md["aliases"] = aliases
        await conn.execute(
            """
            UPDATE memories SET metadata = $1::jsonb, last_used_at = NOW()
            WHERE namespace = $2 AND scope = $3 AND user_id = $4
              AND project = $5 AND key = $6
            """,
            json.dumps(md), SEAT_NAMESPACE, SEAT_SCOPE, SEAT_USER_ID,
            project, mine["key"],
        )
    return {"seat": mine["key"].removeprefix("seat/"), "aliases": aliases}


async def seat_list(project: str | None = None) -> list[dict]:
    """Allocated seats, freshest first. The registry's read side."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT key, project, metadata, last_used_at FROM memories
            WHERE namespace = $1 AND scope = $2
              AND ($3::text IS NULL OR project = $3)
            ORDER BY last_used_at DESC
            """,
            SEAT_NAMESPACE, SEAT_SCOPE,
            project.strip().lower() if project else None,
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
            "aliases": md.get("aliases") or [],
            "session_key": md.get("session_key"),
            "age_seconds": round(age, 1),
            "is_live": age < SEAT_LIVE_SECONDS,
            "reclaimable": age >= SEAT_GRACE_SECONDS,
        })
    return out
