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
    _watcher_state,
)

SEAT_NAMESPACE = INBOX_NAMESPACE

# A seat whose holder beat within this window is LIVE — never reassigned to a
# DIFFERENT session_key. It no longer gates same-key restarts: that used to be
# the window separating "duplicate key" from "genuine restart", and it could
# not do the job, because seconds after a process dies its seat row, its
# presence row and its watcher all still read fresh. See seat_claim.
# Matches the presence staleness threshold so "stale on the roster" and "no
# longer live in the registry" mean the same thing to a reader.
SEAT_LIVE_SECONDS = 600

# A BACKSTOP for deaths nobody reported. INTERNAL TO ALLOCATION — never
# exported (see seat_list).
#
# We asked "what should renew this lease?" and the question was wrong. Renewal
# would have to come from some liveness signal — a heartbeat, a watcher beat —
# and the moment address tenure depends on one, HOLDING AN ADDRESS BECOMES A
# FUNCTION OF BEING AWAKE. That is the entanglement this codebase spent
# 2026-08-01 removing, and it fails the rule that sorted everything else:
# a mailbox does not require its owner to be conscious. So there is no defined
# renewer, by decision (AgentBeast's argument, adopted 2026-08-01).
#
# What actually returns an address, in order:
#   1. EXPLICIT RELEASE on stop — the normal path, live on both providers.
#   2. A goodbye from a dying session — covers hand-launched sessions that no
#      orchestrator ever spawned and therefore nobody can certify dead.
#   3. This backstop, for the remainder: power cut, SIGKILL, machine death.
# The ladder of abandoned `-grok-N` ordinals that originally justified a tight
# window was fixed at its source (release on stop), so (3) is now the rare
# residue rather than the main mechanism.
#
# WHY SO LONG — the asymmetry sets the number, not a guess at how long a
# session lives. A FALSE reclaim is SILENT and takes a working session's
# address out from under it. A LATE reclaim parks one name in a namespace with
# effectively unlimited names, and is VISIBLE the moment an ordinal appears.
# Those costs are nowhere close, so this is tuned to make a false reclaim
# implausible, not to reclaim promptly. Measured before lengthening: one seat
# row per project against MAX_SEAT_ORDINAL=64 — no exhaustion pressure exists
# to trade against.
#
# Reclamation also buys almost nothing to begin with: a session restarting with
# a stable session_key gets its own seat back through the continuity check,
# which has no age condition at all. So this serves exactly one purpose —
# letting a genuinely NEW session reuse an abandoned ordinal to keep numbering
# tight. That is cosmetic, and cosmetics do not outrank a stolen address.
#
# What it risks is not cosmetic. Liveness is inferred from heartbeats, and
# heartbeats only fire on tool calls, so a session doing long uninterrupted
# work looks identical to a dead one — and it is precisely the session you
# least want to disturb. Observed 2026-07-24: a live session, quiet 4.8h while
# running its own build, with its seat reading reclaimable while it was still
# listening. Reclaiming it would have handed its address to a newcomer.
#
# So the window is set past any plausible quiet stretch rather than past a
# plausible pause — and the 2026-08-01 lengthening goes further, because the
# earlier framing ("until a better liveness signal exists") was still looking
# for a renewer. There isn't going to be one. Untidier ordinals, no stolen
# addresses, and the untidiness is bounded by explicit release doing the real
# work.
SEAT_GRACE_SECONDS = 604800  # 7d (was 24h until 2026-08-01)

# Refuse rather than allocate unbounded. A project needing >64 concurrent
# sessions is a misconfiguration (usually a session_key that changes every
# call), and silently minting seat -517 would hide it.
MAX_SEAT_ORDINAL = 64

# How many displaced process nonces a seat remembers (SEAT-9, the one-way
# door). Only needs to outlast a dying predecessor's final heartbeats, so a
# handful is ample; the bound keeps a long-lived seat's metadata from growing
# without limit across many restarts.
MAX_SUPERSEDED_NONCES = 8

# The bridge's generated-key marker (SEAT-16). A session key with this prefix
# was DERIVED from the harness process (pid + start time) because no launcher
# injected one — it names a process, not a stable session handle, so it does
# NOT survive a harness respawn. Served as a FACT (``session_key_generated``)
# on the seats payload so a consumer can weigh continuity claims correctly;
# the verdict — whether to trust such a key across a revive — stays the
# consumer's, per the facts-not-verdicts rule. Mirrors AUTO_KEY_PREFIX in the
# bridge's identity.py; injected keys must never use it.
GENERATED_KEY_PREFIX = "auto-"

# LANE-1 (docs/design/immortal-addresses.md). Providers whose
# `<project>-<provider>` string is an implicit LANE — the immortal mailbox for
# "whoever is/next is the <provider> on <project>". When reservation is on
# (settings.lane_reservation_enabled, default OFF until migration gate (e)),
# the allocator never mints an occupant seat equal to a lane: the lane string
# is an address, occupants are always distinguishable from it (reviewer
# condition, v3: "occupants are NEVER the bare lane string").
#
# Server-side reservation can only enforce lanes the server can DERIVE —
# implicit provider lanes. Repo-declared groups (`groups=` in .engram.cfg)
# live client-side today; they join this predicate if/when groups are
# registered server-side. Admin raw-row writes (PATCH /admin/memories) sit
# outside the allocator by design and are the operator's own hands.
LANE_PROVIDERS = {"claude", "grok", "codex", "cursor", "gpt"}


def is_reserved_lane(seat: str, project: str) -> bool:
    """True when ``seat`` is a lane string for ``project`` and reservation is on.

    Admin stays exempt end-to-end: `admin` is one deliberately-shared role
    (SEAT_EXEMPT_IDENTITIES) and never grows provider lanes — a
    provider-suffixed admin lane would detach maintenance sessions from the
    role again (SEAT-ADMIN-1).
    """
    from server.config import settings
    if not settings.lane_reservation_enabled:
        return False
    if project in SEAT_EXEMPT_IDENTITIES:
        return False
    return any(seat == f"{project}-{p}" for p in LANE_PROVIDERS)


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


def _seat_ordinal(seat: str, base: str) -> int:
    """Sort position for a seat name: the base seat is 1, ``<base>-N`` is N.

    Used to give the continuity lookup a TOTAL ORDER. Lexicographic ordering
    will not do — ``x-claude-10`` sorts before ``x-claude-2`` as text, so a
    session's address would depend on how many siblings it happened to have.
    """
    if seat == base:
        return 1
    if seat.startswith(f"{base}-"):
        suffix = seat[len(base) + 1:]
        if suffix.isdigit():
            return int(suffix)
    return MAX_SEAT_ORDINAL + 1


def _with_park_warning(warning: str | None, preferred: str | None,
                       granted: str, parked_reason: str | None) -> str | None:
    """Compose the GRANT-1(a) advisory onto an existing claim warning.

    Fires only when an explicitly-preferred name was passed over AND the
    grant landed elsewhere — a fallback that reached the preferred name after
    all (or a claim with no preference) stays silent. Additive: rides the
    warning channel every claim consumer already renders.
    """
    if not (preferred and parked_reason and granted != preferred):
        return warning
    park = (
        f"preferred_seat_parked: {preferred!r} was requested but not granted "
        f"— {parked_reason}. Granted {granted!r} instead."
    )
    return f"{warning}; {park}" if warning else park


async def _address_holds_mail(conn, seat: str) -> bool:
    """Would a NEW holder of this address see mail that was not meant for it?

    The hard guard on handing an address to a stranger. Mail for a session that
    died must be preserved for whoever next holds the address, never shown to a
    stranger who happens to be allocated the same ordinal. Correctness (R8)
    outranks tidy numbering (R7), so mail parks the seat indefinitely.

    THE PREDICATE MUST MATCH WHAT A NEW HOLDER ACTUALLY SEES, and the earlier
    one did not. It asked for open mail with ``read_by = []`` — "never read by
    anyone" — which is a strictly narrower set than "visible to a newcomer",
    because ACKS ARE PER-READER: ``inbox_list`` hides a message only from
    readers present in its own ``read_by`` (memory_service.py, the
    ``NOT read_by ? reader_identity`` clause). A message the PREVIOUS holder
    read and acked stays ``open`` and is therefore fully visible to the next
    session allocated that name, while the old predicate reported the address
    clear. So the guard answered a question nobody was asking.

    What a newcomer sees is: not archived (a global hard-hide), and still
    ``open`` (resolved/superseded mail has drained from the default view). That
    is exactly this query, and it is the set the guard has to protect.
    """
    row = await conn.fetchrow(
        """
        SELECT 1 FROM memories
        WHERE namespace = $1 AND scope = $2 AND user_id = $3
          AND COALESCE((metadata->>'archived')::bool, false) = false
          AND COALESCE(metadata->>'status', $4) = $4
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
          host: str | None, superseded: list[str] | None = None,
          runtime: bool = False) -> dict:
    return {
        "kind": "seat",
        "session_key": session_key,
        "session_nonce": session_nonce,
        "provider": provider,
        "host": host,
        "claimed_at": datetime.now(timezone.utc).isoformat(),
        # THE ONE-WAY DOOR (SEAT-9). Nonces that have been displaced from this
        # seat. A claim bearing one of these never gets the seat back — see
        # seat_claim for why a dying predecessor makes that necessary.
        "superseded_nonces": list(superseded or [])[-MAX_SUPERSEDED_NONCES:],
        # ID-2: this seat name was chosen DELIBERATELY at runtime
        # (memory_take_seat), not allocated. Continuity must prefer it over
        # any lower-ordinal row the same session still holds — the seat the
        # session is ACTUALLY on is the fact continuity exists to return.
        "runtime": runtime,
    }


async def seat_claim(
    session_key: str,
    project: str,
    provider: str,
    session_nonce: str | None = None,
    host: str | None = None,
    preferred_seat: str | None = None,
    runtime_seat: bool = False,
) -> dict:
    """Allocate (or re-confirm) this session's unique inbox address.

    Idempotent on ``session_key``: a bridge restart or a harness respawn
    re-claims the SAME seat instead of burning an ordinal, so a session's
    address never changes underneath it or its watcher.

    That idempotency is now unconditional (SEAT-9, newest-wins) and is
    protected by a one-way door rather than by a liveness window — a process
    displaced from a seat can never take it back. Both rules are explained at
    the point of decision below.

    ``runtime_seat=True`` (ID-2) declares that ``preferred_seat`` was chosen
    DELIBERATELY mid-session (memory_take_seat), not resolved from launch
    config — so continuity must MOVE the registration to it rather than
    answer with the seat it already holds. Without this, the registry and the
    tool fought: take_seat moved the bridge, the next heartbeat's claim
    returned the old seat, and the bridge reverted the file the agent had
    just written — two mechanisms answering "who is this session", the loser
    never told. Registering was AgentBeast's call and the right one: a silent
    split is worse than either consistent outcome, and after registration
    continuity returns the seat the session is ACTUALLY on. If the requested
    name is unavailable the claim REFUSES loudly (seat + warning) instead of
    quietly keeping the old name — the caller reverts to the granted seat and
    surfaces the refusal, so both outcomes are consistent and told.

    Returns ``{seat, is_new, reclaimed_from, warning, renamed_from}``.
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

    # LANE-1: a preferred name that is a reserved lane cannot be granted —
    # the string is the immortal mailbox, not a person. This is the
    # gate-(e) SAFETY NET for a straggler old bridge (whose injected
    # identity still means "claim this seat"): it gets a working allocated
    # occupant below plus this explicit notice — degraded-loud, never
    # silently unseated, never a spawn error. A RUNTIME re-seat onto a lane
    # is refused outright further down (deliberate choices get errors, not
    # silent redirects — the ID-2 ruling).
    lane_notice = None
    if preferred and not runtime_seat and is_reserved_lane(preferred, project):
        lane_notice = (
            f"lane_reserved: {preferred!r} is a lane (immortal mailbox for "
            f"this project's {provider} sessions), not a claimable seat; "
            f"allocated an occupant seat instead. Mail to the lane still "
            f"reaches this session — new bridges listen on the lane and are "
            f"addressed at their occupant seat."
        )
        preferred = None

    now = datetime.now(timezone.utc)
    live_cutoff = now - timedelta(seconds=SEAT_LIVE_SECONDS)
    grace_cutoff = now - timedelta(seconds=SEAT_GRACE_SECONDS)

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
        #
        # This lookup MUST consider every row sharing the key, in a stable
        # order. It used to be a bare ``fetchrow`` with no ORDER BY over a
        # predicate that is not unique, which meant a session with more than one
        # seat row got an ARBITRARY one of them — a different one from call to
        # call, as UPDATEs moved tuples around the heap. That produced two
        # distinct live failures (2026-07-26, reproduced on a scratch project):
        #
        #   RUNAWAY (nonce differs): the lookup kept finding the predecessor's
        #   row and never the row this process had just been given, so EVERY
        #   heartbeat fell through to allocation and burned a fresh ordinal —
        #   -3, -4, -5 … on identical input.
        #
        #   OSCILLATION (nonce matches): with two rows carrying one key and one
        #   nonce, whichever row came back was kept — so a running session's
        #   address flipped between them mid-session, and its bridge, its
        #   watcher and its replies each reported a different identity.
        #
        # So: read them all, prefer the row THIS PROCESS holds, and otherwise
        # take the lowest ordinal. Same input, same seat, always.
        held_rows = await conn.fetch(
            """
            SELECT key, metadata, last_used_at FROM memories
            WHERE namespace = $1 AND scope = $2 AND user_id = $3 AND project = $4
              AND metadata->>'session_key' = $5
            """,
            SEAT_NAMESPACE, SEAT_SCOPE, SEAT_USER_ID, project, session_key,
        )
        # A lane_reserved notice (set above) rides the same warning channel
        # every claim consumer already renders — no new field, degraded-loud.
        warning = lane_notice
        if held_rows:
            base = f"{project}-{provider}"
            # A runtime-taken seat outranks any allocated row this session
            # still holds (ID-2): _seat_ordinal sorts non-base names LAST, so
            # without this a renamed session's next claim would find its old
            # ordinal first and hand the old name back — the revert loop this
            # flag exists to end.
            ordered = sorted(
                held_rows,
                key=lambda r: (not _md(r).get("runtime"),
                               _seat_ordinal(r["key"].removeprefix("seat/"), base),
                               r["key"]),
            )
            mine = [
                r for r in ordered
                if session_nonce and _md(r).get("session_nonce") == session_nonce
            ]
            held = mine[0] if mine else ordered[0]
            held_md = _md(held)
            held_nonce = held_md.get("session_nonce")
            superseded = list(held_md.get("superseded_nonces") or [])
            same_process = (
                bool(mine)
                or not session_nonce
                or not held_nonce
                or held_nonce == session_nonce
            )

            # THE ONE-WAY DOOR. A nonce displaced from this seat NEVER regains
            # it. This is what makes newest-wins safe rather than a race.
            #
            # A launcher restarting a session does not wait for the old process
            # to finish dying — AgentBeast's grok path kills a tmux session and
            # starts the replacement with zero wait, so the predecessor may
            # still be exiting while the successor claims. Without this, the
            # dying process's LAST heartbeat would take the address back off
            # the successor that had just been given it: a failure that strikes
            # at random, which is worse than the predictable one it replaces.
            #
            # With the door, the dying tail is harmless by construction rather
            # than by timing. It falls through to allocation, gets an ordinal
            # it will never use, and that row ages out.
            door_closed = (
                bool(session_nonce) and not mine and session_nonce in superseded
            )

            if door_closed:
                warning = (
                    f"seat {held['key'].removeprefix('seat/')!r} was already "
                    f"handed to a newer process for session_key "
                    f"{session_key!r}; this process was displaced and cannot "
                    f"reclaim it. Allocating a separate seat."
                )
            else:
                # NEWEST-WINS. A claim on a known session_key is the same
                # LOGICAL session returning, so it gets its address back —
                # whether or not the previous holder still looks live.
                #
                # This deliberately replaces the older rule, which treated a
                # new nonce on a live-looking holder as two rival sessions and
                # exiled the newcomer to an ordinal. That guard defended
                # against a launcher handing one key to two concurrent
                # workers — a class the key scheme now prevents by
                # construction, since session_key derives from the tmux slot
                # (or ppid + parent start time) and two live workers cannot
                # share a slot except while one is tearing down. It was
                # charging a certain, frequent, user-visible breakage on every
                # respawn to defend a case that can no longer arise, and it
                # sent a huddle invitation to two dead mailboxes on
                # 2026-07-26 to prove it.
                seat = held["key"].removeprefix("seat/")
                if not same_process and held_nonce:
                    superseded.append(held_nonce)
                held_runtime = bool(held_md.get("runtime"))

                # ID-2 RE-SEAT: a runtime-declared name that differs from the
                # held seat MOVES the registration instead of losing to it.
                # Same-process only — a successor inheriting a key does not
                # inherit its predecessor's in-flight rename request.
                if (runtime_seat and preferred and preferred != seat
                        and same_process):
                    # LANE-1: a deliberate rename onto a lane string is
                    # REFUSED loudly (exact-or-refused already governs
                    # take_seat; a lane is never grantable, whatever its
                    # row state).
                    if is_reserved_lane(preferred, project):
                        return {"seat": seat, "is_new": False,
                                "reclaimed_from": None,
                                "warning": (
                                    f"lane_reserved: {preferred!r} is a lane "
                                    f"(immortal mailbox), not a takeable seat; "
                                    f"still registered as {seat!r}. Pick a "
                                    f"different name, or keep this one."
                                ),
                                "renamed_from": None}
                    new_meta = _meta(session_key, session_nonce, provider,
                                     host, superseded, runtime=True)
                    moved = await _try_insert(conn, preferred, project, new_meta)
                    if not moved:
                        # The name exists. Ours already (a prior re-seat, a
                        # race with our own heartbeat) → refresh it and treat
                        # as moved. Anyone else's → refuse; never evict for a
                        # rename, whatever its age — allocation's reclaim
                        # rules exist for allocation, and a deliberate rename
                        # can simply pick another name.
                        other = await conn.fetchrow(
                            """
                            SELECT metadata FROM memories
                            WHERE namespace = $1 AND scope = $2 AND user_id = $3
                              AND project = $4 AND key = $5
                            """,
                            SEAT_NAMESPACE, SEAT_SCOPE, SEAT_USER_ID,
                            project, f"seat/{preferred}",
                        )
                        if (other is not None
                                and _md(other).get("session_key") == session_key):
                            await conn.execute(
                                """
                                UPDATE memories
                                SET metadata = $1::jsonb, last_used_at = NOW()
                                WHERE namespace = $2 AND scope = $3
                                  AND user_id = $4 AND project = $5 AND key = $6
                                """,
                                json.dumps(new_meta),
                                SEAT_NAMESPACE, SEAT_SCOPE, SEAT_USER_ID,
                                project, f"seat/{preferred}",
                            )
                            moved = True
                    if moved:
                        # Free the old name — unless it still holds
                        # undelivered mail (R8: never hand a stranger a seat
                        # with mail; the row ages out instead, and the
                        # duplicate-collapse below cleans it once drained).
                        if not await _address_holds_mail(conn, seat):
                            await conn.execute(
                                """
                                DELETE FROM memories
                                WHERE namespace = $1 AND scope = $2
                                  AND user_id = $3 AND project = $4 AND key = $5
                                """,
                                SEAT_NAMESPACE, SEAT_SCOPE, SEAT_USER_ID,
                                project, held["key"],
                            )
                        return {"seat": preferred, "is_new": False,
                                "reclaimed_from": None, "warning": None,
                                "renamed_from": seat}
                    # REFUSED — loudly, per the ID-2 ruling: an error the
                    # session can act on, never a no-op. The caller reverts to
                    # the granted seat so bridge, watcher and registry agree.
                    warning = (
                        f"runtime seat {preferred!r} is unavailable (held by "
                        f"another session); still registered as {seat!r}. "
                        f"Pick a different name, or keep this one."
                    )

                await conn.execute(
                    """
                    UPDATE memories SET metadata = $1::jsonb, last_used_at = NOW()
                    WHERE namespace = $2 AND scope = $3 AND user_id = $4
                      AND project = $5 AND key = $6
                    """,
                    json.dumps(
                        _meta(session_key, session_nonce, provider, host,
                              superseded,
                              # A registered runtime seat stays runtime across
                              # ordinary refreshes — including from a restarted
                              # bridge that no longer knows it took one (the
                              # flag lives here precisely because process
                              # memory does not survive).
                              runtime=held_runtime or (runtime_seat
                                                       and preferred == seat))
                    ),
                    SEAT_NAMESPACE, SEAT_SCOPE,
                    SEAT_USER_ID, project, held["key"],
                )
                # Collapse the duplicates this session accumulated while the
                # lookup was non-deterministic. One session holds ONE seat per
                # project — the invariant the UNIQUE index never expressed,
                # because it constrains the seat NAME, not the claimant.
                #
                # R8 still outranks tidiness: a duplicate holding undelivered
                # mail is left alone rather than freed for a stranger to be
                # allocated and read. It ages out through normal reclamation.
                for row in ordered:
                    if row["key"] == held["key"]:
                        continue
                    if _md(row).get("session_nonce") != session_nonce:
                        continue  # not provably mine — never free another's seat
                    dupe = row["key"].removeprefix("seat/")
                    if await _address_holds_mail(conn, dupe):
                        continue
                    await conn.execute(
                        """
                        DELETE FROM memories
                        WHERE namespace = $1 AND scope = $2 AND user_id = $3
                          AND project = $4 AND key = $5
                        """,
                        SEAT_NAMESPACE, SEAT_SCOPE, SEAT_USER_ID,
                        project, row["key"],
                    )
                return {"seat": seat, "is_new": False,
                        "reclaimed_from": None, "warning": warning,
                        "renamed_from": None}

        # 2-5. Allocate: first free candidate, then first reclaimable one.
        #
        # GRANT-1(a): when the loop passes OVER an explicitly-preferred name,
        # the caller must hear it. Found live twice (2026-08-16 "two Beast
        # Chats", 2026-08-17 "AB vs AB-App"): a launcher-injected preference
        # was parked by R8 and the claim fell to a project-lane ordinal with
        # warning:null, so the team's session was exiled from its own name in
        # silence and the picker lost the only string that distinguished it.
        # The parking is CORRECT (mail must never reach a stranger); the
        # silence is the defect (ADDR-2 doctrine). Record why the preferred
        # candidate was skipped and say so on the existing warning channel.
        #
        # Only a DISTINCTIVE preference is loud. When the preference IS the
        # conventional base name (every launcher computes <project>-<provider>
        # for every session), falling to an ordinal is ordinary allocation —
        # a colleague holds the base — and warning on each such claim would
        # bury the real signal. Losing a distinctive name loses identity;
        # losing the base loses nothing but a number.
        parked_reason = None
        base = f"{project}-{provider}"
        distinct_preferred = preferred if preferred != base else None
        for seat in seat_candidates(project, provider, preferred):
            # LANE-1: reserved lane strings are never minted as occupant
            # seats — skip before BOTH the insert and the takeover branch
            # (a takeover would re-mint a not-yet-drained lane-named row as
            # an occupant, which is the same defect through the other door).
            # With reservation on, the base candidate IS the lane, so first
            # occupants allocate from `<base>-2` upward; the ADDR-3 kind
            # marker is what keeps surfaces rendering that correctly.
            if is_reserved_lane(seat, project):
                continue
            # The runtime flag marks a DELIBERATELY-CHOSEN name. It applies
            # only when the granted candidate IS the requested name — a
            # fallback to base/ordinal is an allocation, not a choice.
            meta = _meta(session_key, session_nonce, provider, host,
                         runtime=runtime_seat and seat == preferred)

            # R8 ON THE FREE PATH. A name with NO seat row can still hold mail,
            # and until 2026-08-13 nothing checked: the guard below at the
            # takeover branch only runs when a row still exists, and
            # ``seat_release`` DELETEs the row without consulting the inbox at
            # all. So the whole clean-shutdown path — the one a launcher drives
            # on every despawn — freed a name and let the next claimant INSERT
            # into it and read a stranger's mail. Inbox rows key on the ADDRESS
            # STRING, not on the seat row, so dropping the row moves nothing.
            #
            # The slow path was guarded and the fast path added later was not:
            # explicit release short-circuits the 7d grace that made takeover
            # the only way a used name changed hands, and the guard never came
            # with it.
            #
            # RESIDUAL, stated rather than papered over: this is check-then-act
            # on a bare connection (see the acquire above — no transaction), so
            # mail arriving between the check and the insert still lands on the
            # new holder. That window is milliseconds against the unbounded one
            # it replaces, and it cannot be closed by a transaction anyway —
            # nothing serialises an INSERT by a different session against our
            # read. Closing it fully would need the address space locked, which
            # costs more than the residue is worth.
            if await _address_holds_mail(conn, seat):
                if seat == distinct_preferred:
                    parked_reason = (
                        "it holds open mail a new holder would see (R8 parks "
                        "a used name rather than hand it to a stranger); "
                        "drain the open rows on that address (read them, then "
                        "resolve/archive) and the next claim can take the name"
                    )
                continue  # R8 outranks tidy numbering: park it, take the next

            if await _try_insert(conn, seat, project, meta):
                # LANE-4: a newly allocated occupant may inherit the lane's
                # read-cursor (succession) — see _apply_lane_inheritance for
                # the conditions and the live-colleague block.
                await _apply_lane_inheritance(conn, seat, project, provider,
                                              host, session_key)
                return {"seat": seat, "is_new": True,
                        "reclaimed_from": None,
                        "warning": _with_park_warning(
                            warning, distinct_preferred, seat,
                            parked_reason)}

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
                if seat == distinct_preferred:
                    parked_reason = "a live session currently holds it"
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
                if seat == distinct_preferred:
                    parked_reason = ("its previous holder is inside the "
                                     "reclaim grace window")
                continue
            if await _address_holds_mail(conn, seat):
                if seat == distinct_preferred:
                    parked_reason = (
                        "it holds open mail a new holder would see (R8 parks "
                        "a used name rather than hand it to a stranger); "
                        "drain the open rows on that address (read them, then "
                        "resolve/archive) and the next claim can take the name"
                    )
                continue  # R8: never hand a stranger someone else's mail
            if await _presence_is_fresh(conn, seat, project):
                if seat == distinct_preferred:
                    parked_reason = ("a session is actively heartbeating "
                                     "at it")
                continue  # SEAT-8: an independently-live session holds this

            if await _try_takeover(conn, seat, project, meta,
                                   older_than=live_cutoff):
                await _apply_lane_inheritance(conn, seat, project, provider,
                                              host, session_key)
                return {
                    "seat": seat,
                    "is_new": True,
                    "reclaimed_from": prior.get("session_key"),
                    "warning": _with_park_warning(
                        warning, distinct_preferred, seat, parked_reason),
                }

    raise ValueError(
        f"no free seat for project {project!r} provider {provider!r} after "
        f"{MAX_SEAT_ORDINAL} candidates — this almost always means the caller's "
        f"session_key changes on every claim rather than that {MAX_SEAT_ORDINAL} "
        f"sessions are genuinely live."
    )


DEATH_SCOPE = "death"
LANE_CURSOR_SCOPE = "lane_cursor"


async def death_certify(
    session_key: str,
    seat: str,
    lane: str,
    project: str,
    provider: str,
    host: str,
    died_at,
    cause: str,
    graceful: bool | None,
    certified_by: str | None,
) -> dict:
    """LANE-4: record a spawner's death certificate and feed the lane cursor.

    The store never infers death — this is the intake for the SPAWNER's
    verdict (the party that performed or observed the kill). The certificate
    is accepted even while the presence row still looks live: a heartbeat can
    outlive a kill, it can never observe one (PICK-REG-1b, imported).

    Effects, deliberately narrow: (1) a durable cert row; (2) the lane
    read-cursor gains the union of the dead occupant's acks on addresses
    that outlive sessions, so a certified SUCCESSOR can inherit read-state
    instead of drowning in a predecessor's history. It does NOT free the
    seat or accelerate reclamation — SEAT-13 stays the owner's question.

    Idempotent on session_key, falling back to (seat, died_at) when the
    spawner never had a key (SEAT-6). The cursor union re-runs on repeats —
    it is idempotent by construction, which also heals a cert that stored
    but failed mid-harvest.
    """
    project = (project or "").strip().lower()
    idem = session_key or f"{seat}|{died_at.isoformat()}"
    key = f"{DEATH_SCOPE}/{idem}"
    meta = {
        "kind": "death",
        "session_key": session_key,
        "seat": seat,
        "lane": lane,
        "provider": provider,
        "host": host,
        "died_at": died_at.isoformat(),
        "cause": cause,
        "graceful": graceful,
        "certified_by": certified_by,
        "received_at": datetime.now(timezone.utc).isoformat(),
    }
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO memories
                (namespace, key, value, scope, user_id, project,
                 tags, tags_search, search_text, embedding, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, '', '', $3, NULL, $7::jsonb)
            ON CONFLICT (namespace, key, scope, user_id, project) DO NOTHING
            RETURNING key
            """,
            SEAT_NAMESPACE, key, idem, DEATH_SCOPE, SEAT_USER_ID,
            project, json.dumps(meta),
        )
        created = row is not None

        # Cert-beats-heartbeat, recorded as a FACT consumers may weigh: the
        # seat row (if it still exists) is marked, never deleted or aged.
        if seat:
            await conn.execute(
                """
                UPDATE memories
                SET metadata = metadata || '{"death_certified": true}'::jsonb
                WHERE namespace = $1 AND scope = $2 AND user_id = $3
                  AND project = $4 AND key = $5
                """,
                SEAT_NAMESPACE, SEAT_SCOPE, SEAT_USER_ID, project,
                f"seat/{seat}",
            )

        cursor_updated = False
        cursor_size = 0
        # No seat → no reader identity → nothing to harvest (PM ruling: do
        # NOT harvest seat@% from an empty seat). Lane alone still records.
        if lane and seat:
            if host:
                harvested = await conn.fetch(
                    """
                    SELECT key FROM memories
                    WHERE scope = 'inbox'
                      AND COALESCE(metadata->'read_by','[]'::jsonb) ? $1::text
                      AND user_id <> $2
                      AND user_id NOT LIKE 'machine:%'
                    """,
                    f"{seat}@{host}", seat,
                )
            else:
                harvested = await conn.fetch(
                    """
                    SELECT key FROM memories
                    WHERE scope = 'inbox'
                      AND EXISTS (
                        SELECT 1 FROM jsonb_array_elements_text(
                            COALESCE(metadata->'read_by','[]'::jsonb)) r
                        WHERE r LIKE $1
                      )
                      AND user_id <> $2
                      AND user_id NOT LIKE 'machine:%'
                    """,
                    f"{seat}@%", seat,
                )
            ids = [r["key"] for r in harvested]
            cur = await conn.fetchrow(
                """
                SELECT metadata FROM memories
                WHERE namespace = $1 AND scope = $2 AND user_id = $3
                  AND project = $4 AND key = $5
                """,
                SEAT_NAMESPACE, LANE_CURSOR_SCOPE, SEAT_USER_ID, project,
                f"cursor/{lane}",
            )
            existing = set((_md(cur).get("ids") or []) if cur else [])
            merged = sorted(existing | set(ids))
            cursor_size = len(merged)
            if cur is None:
                await conn.execute(
                    """
                    INSERT INTO memories
                        (namespace, key, value, scope, user_id, project,
                         tags, tags_search, search_text, embedding, metadata)
                    VALUES ($1, $2, $3, $4, $5, $6, '', '', $3, NULL,
                            $7::jsonb)
                    ON CONFLICT (namespace, key, scope, user_id, project)
                    DO UPDATE SET metadata = EXCLUDED.metadata
                    """,
                    SEAT_NAMESPACE, f"cursor/{lane}", lane,
                    LANE_CURSOR_SCOPE, SEAT_USER_ID, project,
                    json.dumps({"kind": "lane_cursor", "ids": merged,
                                "updated_at": meta["received_at"],
                                "last_cert": key, "applied": []}),
                )
            else:
                md = _md(cur)
                md["ids"] = merged
                md["updated_at"] = meta["received_at"]
                md["last_cert"] = key
                await conn.execute(
                    """
                    UPDATE memories SET metadata = $1::jsonb
                    WHERE namespace = $2 AND scope = $3 AND user_id = $4
                      AND project = $5 AND key = $6
                    """,
                    json.dumps(md), SEAT_NAMESPACE, LANE_CURSOR_SCOPE,
                    SEAT_USER_ID, project, f"cursor/{lane}",
                )
            cursor_updated = True
    return {"created": created, "cursor_updated": cursor_updated,
            "cursor_size": cursor_size}


async def _apply_lane_inheritance(conn, seat: str, project: str,
                                  provider: str, host: str | None,
                                  session_key: str) -> None:
    """Materialize the lane cursor into a new occupant's read-state.

    Runs at claim time for a NEWLY allocated occupant. Inheritance fires iff
    (a) no OTHER occupant of this lane has fresh presence at claim — the
    lane is empty, this is succession; or (b) the claimant's session_key
    matches a certified death — the same logical session returning. A live
    colleague on the lane blocks it cold: per-reader acks stay per-reader
    (the Problem-1 rule; inheriting here would steal a living session's
    unread mail).

    NAMED RESIDUAL (PM, accepted): the colleague check is presence
    freshness. A head-down occupant past the freshness window looks like an
    empty lane and the newcomer inherits. Same liveness split as the roster;
    no second death oracle is invented here.

    Application appends the new reader to read_by — the existing read
    semantics, nothing parallel — and records {reader, cert, at} on the
    cursor row so forensics can tell inherited from actually-read.
    """
    if not host:
        return  # no host → no reader identity to materialize under
    lane = f"{project}-{provider}"
    cur = await conn.fetchrow(
        """
        SELECT metadata FROM memories
        WHERE namespace = $1 AND scope = $2 AND user_id = $3
          AND project = $4 AND key = $5
        """,
        SEAT_NAMESPACE, LANE_CURSOR_SCOPE, SEAT_USER_ID, project,
        f"cursor/{lane}",
    )
    if cur is None:
        return
    md = _md(cur)
    ids = md.get("ids") or []
    if not ids:
        return

    same_session = False
    if session_key:
        cert = await conn.fetchrow(
            """
            SELECT 1 FROM memories
            WHERE namespace = $1 AND scope = $2 AND user_id = $3
              AND project = $4 AND metadata->>'session_key' = $5
            """,
            SEAT_NAMESPACE, DEATH_SCOPE, SEAT_USER_ID, project, session_key,
        )
        same_session = cert is not None
    if not same_session:
        peers = await conn.fetch(
            """
            SELECT key FROM memories
            WHERE namespace = $1 AND scope = $2 AND user_id = $3
              AND project = $4 AND metadata->>'provider' = $5 AND key <> $6
            """,
            SEAT_NAMESPACE, SEAT_SCOPE, SEAT_USER_ID, project, provider,
            f"seat/{seat}",
        )
        for p in peers:
            peer_seat = p["key"].removeprefix("seat/")
            if await _presence_is_fresh(conn, peer_seat, project):
                return  # live colleague on the lane — never inherit

    reader = f"{seat}@{host}"
    await conn.execute(
        """
        UPDATE memories
        SET metadata = jsonb_set(
            metadata, '{read_by}',
            COALESCE(metadata->'read_by','[]'::jsonb) || to_jsonb($1::text))
        WHERE scope = 'inbox' AND key = ANY($2::text[])
          AND NOT COALESCE(metadata->'read_by','[]'::jsonb) ? $1::text
        """,
        reader, ids,
    )
    applied = list(md.get("applied") or [])
    applied.append({"reader": reader,
                    "cert": md.get("last_cert"),
                    "at": datetime.now(timezone.utc).isoformat()})
    md["applied"] = applied[-16:]
    await conn.execute(
        """
        UPDATE memories SET metadata = $1::jsonb
        WHERE namespace = $2 AND scope = $3 AND user_id = $4
          AND project = $5 AND key = $6
        """,
        json.dumps(md), SEAT_NAMESPACE, LANE_CURSOR_SCOPE, SEAT_USER_ID,
        project, f"cursor/{lane}",
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
        # SEATS-1: join each seat to its presence row (`presence/<seat>` under
        # the same project) so the payload can carry the watcher's beat. The
        # roster has served these since a70c67d; a consumer building a picker
        # for sessions on OTHER boxes had to fall back to a coarser signal.
        rows = await conn.fetch(
            """
            SELECT s.key, s.project, s.metadata, s.last_used_at,
                   p.metadata AS presence_metadata
            FROM memories s
            LEFT JOIN memories p
              ON p.namespace = s.namespace
             AND p.scope = $5
             AND p.user_id = s.project
             AND p.key = 'presence/' || substr(s.key, 6)
            WHERE s.namespace = $1 AND s.scope = $2
              AND ($3::text IS NULL OR s.project = $3)
              AND ($4::text IS NULL OR s.metadata->>'session_key' = $4)
            ORDER BY s.last_used_at DESC
            """,
            SEAT_NAMESPACE, SEAT_SCOPE,
            project.strip().lower() if project else None,
            session_key.strip().lower() if session_key else None,
            PRESENCE_SCOPE,
        )
    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        md = _md(r)
        age = (now - r["last_used_at"]).total_seconds()
        key = md.get("session_key")
        # SEATS-1: the watcher's beat, in the roster's exact three-valued
        # vocabulary (_watcher_state): True = beat within the freshness
        # window, mail will wake it; False = a watcher HAS beaten here and
        # went quiet; None = no watcher has ever beaten — no basis. None is
        # NEVER coerced to False: absent is not dead, and if a mixed fleet
        # recurs (pre-beat bridges on some boxes) the agreed contract is
        # exactly this — null means "no beat exists there", distinguishable
        # from "beat missed".
        pmd = r["presence_metadata"]
        if isinstance(pmd, str):
            pmd = json.loads(pmd)
        watcher_alive, watcher_seen = _watcher_state(pmd or {}, now)
        out.append({
            "seat": r["key"].removeprefix("seat/"),
            "project": r["project"],
            "provider": md.get("provider"),
            "host": md.get("host"),
            "session_key": key,
            # SEAT-16: a generated key names a PROCESS and dies with it — a
            # harness respawn arrives as a NEW key and a NEW seat. Serving
            # the distinction is what lets a consumer stop assuming every
            # key survives a revive. A fact about how the key was minted,
            # not a liveness verdict.
            "session_key_generated": bool(
                key and key.startswith(GENERATED_KEY_PREFIX)
            ),
            # `age_seconds` is the whole answer. Both flags that used to sit
            # here are gone (2026-08-01): `is_live` (age < 600) and then
            # `reclaimable` (age >= grace). Each was a THRESHOLD APPLIED TO
            # THIS EXACT NUMBER — so exporting them shipped the same bit twice,
            # once as a fact and once as a verdict, and it was the verdict half
            # that invited every consumer to adopt our threshold as truth. A
            # peer's reaper leaned on it for weeks, which meant our tuning
            # silently became their policy.
            #
            # The backstop still exists; it is INTERNAL TO ALLOCATION, where we
            # legitimately own the address space and must decide who gets a
            # free name. Callers that want a judgement now apply their own to
            # age_seconds, which is the correct place for it — same move as
            # dropping `state` from the roster, one layer up.
            "age_seconds": round(age, 1),
            # SEATS-1 (requested by AgentBeast 2026-07-28). Unlike is_live/
            # reclaimable these are not thresholds over age_seconds — the beat
            # is an independent SIGNAL (the watcher process speaking), ~2×
            # sharper than presence on an ungraceful death (measured: ≈4m24s
            # vs ≈9m24s to go stale). Same fields, same vocabulary, same
            # freshness window as the roster.
            "watcher_alive": watcher_alive,
            "watcher_last_seen": watcher_seen.isoformat() if watcher_seen else None,
        })
    return out
