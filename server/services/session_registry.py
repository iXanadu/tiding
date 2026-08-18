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
    ASK_INTENTS,
    INBOX_NAMESPACE,
    INBOX_OPEN,
    INBOX_RESOLVED,
    INBOX_SCOPE,
    PRESENCE_SCOPE,
    SEAT_EXEMPT_IDENTITIES,
    SEAT_SCOPE,
    SEAT_USER_ID,
    _mark_handled,
    _row_to_inbox_message,
    _watcher_state,
)

SEAT_NAMESPACE = INBOX_NAMESPACE

# Step 8 (build-plan): the project REGISTRY — the address tree's verifiable
# root. A project registers itself on first contact (every session claims,
# so the claim path is the census); projects that predate the registry are
# listed from their seat rows meanwhile and register organically on their
# next claim. Dormancy shares the seat-grace clock deliberately (see
# PROJECT_DORMANT_SECONDS below, after SEAT_GRACE_SECONDS is defined).
PROJECT_REGISTRY_SCOPE = "project-root"

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

# One clock, two questions: a project none of whose sessions could still
# hold a seat is dormant by the same rule that frees the seats.
PROJECT_DORMANT_SECONDS = SEAT_GRACE_SECONDS

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


def is_reserved_root(seat: str, project: str) -> bool:
    """True when ``seat`` IS the bare project string and reservation is on.

    Step 9 / O4: bare ``{proj}`` is the channel — the immortal root — and
    never grantable as a seat. Allocation never yields it (seat_candidates
    starts at the lane base), but the runtime/preferred path only
    lane-checked, so ``memory_take_seat(name="<project>")`` could still
    squat the root — the exact ``seat/engram`` cursor-corpse class the
    outcomes review measured. Cross-project roots are caught against the
    Step-8 registry at claim time (async, see seat_claim).
    """
    from server.config import settings
    if not settings.lane_reservation_enabled:
        return False
    if project in SEAT_EXEMPT_IDENTITIES:
        return False
    return seat == project


def allocation_decision(*, root: bool, lane: bool, age: float | None,
                        holds_mail: bool, presence_fresh: bool,
                        last_used_at=None) -> dict:
    """THE skip ladder — the single copy (ADDR-REG-1).

    Consulted by BOTH seat_claim (which gathers facts per candidate and
    performs the actions) and the register's ``_allocation`` (which maps its
    batch-read columns onto the same facts). Two copies of this ORDER is how
    the register starts lying about what the allocator would do; the
    fact-gathering necessarily differs (point-reads vs batch), the decision
    must not.

    Ladder: root -> lane -> (no row: mail parks, else free) -> live ->
    grace -> mail -> fresh presence -> free.
    """
    if root:
        return {"would_skip": True, "reason": "reserved-root",
                "grace_expires_at": None}
    if lane:
        return {"would_skip": True, "reason": "reserved-lane",
                "grace_expires_at": None}
    if age is None:
        if holds_mail:
            return {"would_skip": True, "reason": "mail-parked",
                    "grace_expires_at": None}
        return {"would_skip": False, "reason": None, "grace_expires_at": None}
    if age < SEAT_LIVE_SECONDS:
        return {"would_skip": True, "reason": "live-holder",
                "grace_expires_at": None}
    if age < SEAT_GRACE_SECONDS:
        expires = (last_used_at + timedelta(seconds=SEAT_GRACE_SECONDS)
                   if last_used_at else None)
        return {"would_skip": True, "reason": "grace-window",
                "grace_expires_at":
                    expires.isoformat() if expires else None}
    if holds_mail:
        return {"would_skip": True, "reason": "mail-parked",
                "grace_expires_at": None}
    if presence_fresh:
        return {"would_skip": True, "reason": "presence-fresh",
                "grace_expires_at": None}
    return {"would_skip": False, "reason": None, "grace_expires_at": None}


# GRANT-1(a)'s loud-park texts, keyed by the ladder's own reason strings so
# the warning a claimant reads and the reason the register serves can never
# say different things about the same skip.
_PARKED_REASON_TEXT = {
    "live-holder": "a live session currently holds it",
    "grace-window": "its previous holder is inside the reclaim grace window",
    "mail-parked": (
        "it holds open mail a new holder would see (R8 parks a used name "
        "rather than hand it to a stranger); drain the open rows on that "
        "address (read them, then resolve/archive) and the next claim can "
        "take the name"
    ),
    "presence-fresh": "a session is actively heartbeating at it",
    "reserved-root": "it is a project root (channel), never a seat",
    "reserved-lane": "it is a lane (immortal mailbox), never a seat",
}


async def _fetch_known_roots(conn) -> set[str]:
    """Every project root the store knows — registered on the claim census
    OR observed via seat rows (the Step-9 audit amendment: registered-only
    would leave a root squattable exactly while its registry row waits for
    the project's next claim). Exempt roles are not roots."""
    rows = await conn.fetch(
        """
        SELECT DISTINCT project FROM memories
        WHERE namespace = $1 AND scope IN ($2, $3)
          AND project IS NOT NULL
        """,
        SEAT_NAMESPACE, PROJECT_REGISTRY_SCOPE, SEAT_SCOPE,
    )
    return {r["project"] for r in rows} - SEAT_EXEMPT_IDENTITIES


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
          runtime: bool = False, preferred: str | None = None) -> dict:
    return {
        "kind": "seat",
        "session_key": session_key,
        "session_nonce": session_nonce,
        "provider": provider,
        "host": host,
        # ADDR-REG: the name this claim ASKED for, recorded on the granted row
        # so the register can serve "wanted agentbeast-app-grok, got grok-6"
        # instead of losing the request the moment the claim response scrolls
        # by. None on rows written before the field existed means UNRECORDED —
        # a consumer must never render it as "no preference".
        "preferred_seat": preferred,
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
    elif preferred and not runtime_seat and is_reserved_root(preferred,
                                                             project):
        # Step 9: the bare project string is the CHANNEL — the immortal
        # root — never a seat. Same degraded-loud shape as the lane net.
        lane_notice = (
            f"root_reserved: {preferred!r} is this project's channel (the "
            f"immortal root), not a claimable seat; allocated an occupant "
            f"seat instead. Mail to the channel still reaches this session."
        )
        preferred = None

    now = datetime.now(timezone.utc)
    live_cutoff = now - timedelta(seconds=SEAT_LIVE_SECONDS)
    grace_cutoff = now - timedelta(seconds=SEAT_GRACE_SECONDS)

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Step 8: the project root registers itself on first contact. Every
        # session claims, so this IS the census; claims are heartbeat-
        # throttled, so the upsert is cheap. Best-effort — the registry is
        # an observation, never a gate on the claim.
        try:
            await conn.execute(
                """
                INSERT INTO memories (namespace, key, value, scope, user_id,
                                      project, tags, tags_search, metadata,
                                      last_used_at)
                VALUES ($1, $2, 'project-root', $3, $4, $5, '', '',
                        jsonb_build_object('kind', 'project-root'), NOW())
                ON CONFLICT (namespace, key, scope, user_id, project)
                  DO UPDATE SET last_used_at = NOW()
                """,
                SEAT_NAMESPACE, f"project/{project}",
                PROJECT_REGISTRY_SCOPE, SEAT_USER_ID, project,
            )
        except Exception:
            pass
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
                    # LANE-1 + Step 9: a deliberate rename onto a lane OR a
                    # known project ROOT is REFUSED loudly (exact-or-refused
                    # already governs take_seat; reserved strings are never
                    # grantable, whatever their row state). Roots checked
                    # against the full known set — registered or observed —
                    # per the audit amendment.
                    if (is_reserved_lane(preferred, project)
                            or is_reserved_root(preferred, project)
                            or preferred in await _fetch_known_roots(conn)):
                        # Prefixes are shipped strings (WIRE-1: deployed
                        # readers may match them) — lanes keep the exact
                        # prefix that shipped; roots get their own.
                        if is_reserved_lane(preferred, project):
                            prefix, kind = ("lane_reserved",
                                            "a lane (immortal mailbox)")
                        else:
                            prefix, kind = ("root_reserved",
                                            "a project root (channel)")
                        return {"seat": seat, "is_new": False,
                                "reclaimed_from": None,
                                "warning": (
                                    f"{prefix}: {preferred!r} is {kind}, "
                                    f"not a takeable seat; still registered "
                                    f"as {seat!r}. Pick a different name, "
                                    f"or keep this one."
                                ),
                                "renamed_from": None}
                    # The taken name IS the ask — record it, or the move
                    # erases the row's grant-time preference and the register
                    # reads a deliberate rename as UNRECORDED (measured live
                    # 2026-08-18: agentbeast-app-grok's moved row lost it).
                    new_meta = _meta(session_key, session_nonce, provider,
                                     host, superseded, runtime=True,
                                     preferred=preferred)
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
                                                       and preferred == seat),
                              # Carry the grant-time ask forward; a refresh
                              # must not erase what the original claim wanted.
                              preferred=held_md.get("preferred_seat")
                              or (preferred if preferred and preferred != base
                                  else None))
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
        # Step 9 (audit amendment): ANY known root — registered on the claim
        # census OR merely observed via seat rows — is reserved. Registered-
        # only would leave take_seat("engram") open exactly while engram's
        # own registry row waits for its next claim (measured registered=
        # false minutes before this shipped). One fetch per allocation, not
        # per candidate.
        known_roots = await _fetch_known_roots(conn)
        for seat in seat_candidates(project, provider, preferred):
            # LANE-1 + Step 9: reserved lane AND root strings are never
            # minted as occupant seats — skip before BOTH the insert and the
            # takeover branch (a takeover would re-mint a not-yet-drained
            # reserved-named row as an occupant, the same defect through the
            # other door). With reservation on, the base candidate IS the
            # lane, so first occupants allocate from `<base>-2` upward.
            if (is_reserved_lane(seat, project)
                    or is_reserved_root(seat, project)
                    or seat in known_roots):
                if seat == distinct_preferred:
                    parked_reason = _PARKED_REASON_TEXT["reserved-root"] \
                        if not is_reserved_lane(seat, project) \
                        else _PARKED_REASON_TEXT["reserved-lane"]
                continue
            # The runtime flag marks a DELIBERATELY-CHOSEN name. It applies
            # only when the granted candidate IS the requested name — a
            # fallback to base/ordinal is an allocation, not a choice.
            meta = _meta(session_key, session_nonce, provider, host,
                         runtime=runtime_seat and seat == preferred,
                         preferred=distinct_preferred)

            # THE LADDER, one copy (ADDR-REG-1): facts gathered here, the
            # DECISION made by allocation_decision — the same function the
            # register serves, so the two can never drift. The load-bearing
            # history lives with the function and stays true here:
            # R8 on the free path (a name with NO row can still hold mail;
            # seat_release bare-DELETEs, so the clean-shutdown path once
            # handed a stranger a dead session's unread mail — 2026-08-13);
            # takeover on ONE condition, past the full grace window (the
            # "same slot" shortcut was a live-address steal, dropped
            # 2026-07-24); check-then-act residual stated not papered over:
            # no transaction can serialise a foreign INSERT against our
            # read, so a milliseconds window remains, against the unbounded
            # one it replaced.
            row = await conn.fetchrow(
                """
                SELECT metadata, last_used_at FROM memories
                WHERE namespace = $1 AND scope = $2 AND user_id = $3
                  AND project = $4 AND key = $5
                """,
                SEAT_NAMESPACE, SEAT_SCOPE, SEAT_USER_ID, project, f"seat/{seat}",
            )
            age = ((now - row["last_used_at"]).total_seconds()
                   if row is not None else None)
            holds_mail = await _address_holds_mail(conn, seat)
            # Presence only decides at the last rung — don't pay the query
            # on candidates the earlier rungs already park.
            presence_fresh = False
            if (row is not None and age is not None
                    and age >= SEAT_GRACE_SECONDS and not holds_mail):
                presence_fresh = await _presence_is_fresh(conn, seat, project)
            d = allocation_decision(
                root=False, lane=False, age=age, holds_mail=holds_mail,
                presence_fresh=presence_fresh,
                last_used_at=row["last_used_at"] if row is not None else None,
            )
            if d["would_skip"]:
                if seat == distinct_preferred:
                    parked_reason = _PARKED_REASON_TEXT.get(
                        d["reason"], d["reason"])
                continue
            if row is None:
                if await _try_insert(conn, seat, project, meta):
                    # LANE-4: a newly allocated occupant may inherit the
                    # lane's read-cursor (succession) — see
                    # _apply_lane_inheritance for the conditions.
                    await _apply_lane_inheritance(conn, seat, project,
                                                  provider, host, session_key)
                    return {"seat": seat, "is_new": True,
                            "reclaimed_from": None,
                            "warning": _with_park_warning(
                                warning, distinct_preferred, seat,
                                parked_reason)}
                # Raced: a row appeared between decision and insert. It is
                # by construction a fresh (live) holder — the old ladder
                # re-read and parked it at the live rung; next candidate.
                continue
            prior = _md(row)
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


async def address_register(project: str | None = None) -> list[dict]:
    """ADDR-REG: every name the store is holding, and why — the owner's view.

    The register the roster is not: the roster answers "who is speaking",
    this answers "which names are OCCUPIED", live or corpse, including names
    with no seat row at all that R8 parks because they hold open mail (the
    class that is invisible to /session/seats and cost GRANT-1 two bites).

    Facts, never verdicts — with ONE deliberate exception: the ``allocation``
    block reports what THIS allocator would do with the name right now
    (skip and why, per its own constants). That is not a liveness verdict on
    the session; it is engram reporting its own policy, which only engram can
    do and which is exactly the "why is this name unavailable" question the
    register exists to answer.

    Honesty limits, part of the wire contract:
    - ``preferred_seat`` null = UNRECORDED (pre-field row), never "no
      preference".
    - Death is EVIDENCE, not a field: ``farewell_at`` (a watcher observed the
      exit; voided by later life) and/or ``death`` (a spawner's certificate).
      Absence of both means "no death evidence", never "alive".
    - ``claimed_at`` null = the row predates the field.
    - Mail-only entries carry no project (an inbox address is a bare string);
      when a ``project`` filter is given they are included by prefix
      heuristic (``<project>`` / ``<project>-*``), documented as such.
    - '#'-channel addresses are excluded: they are never allocatable names.
    """
    project = project.strip().lower() if project else None
    now = datetime.now(timezone.utc)
    pool = await get_pool()
    async with pool.acquire() as conn:
        seat_rows = await conn.fetch(
            """
            SELECT s.key, s.project, s.metadata, s.last_used_at,
                   p.metadata AS presence_metadata,
                   p.last_used_at AS presence_last_used_at
            FROM memories s
            LEFT JOIN memories p
              ON p.namespace = s.namespace
             AND p.scope = $4
             AND p.user_id = s.project
             AND p.key = 'presence/' || substr(s.key, 6)
            WHERE s.namespace = $1 AND s.scope = $2
              AND ($3::text IS NULL OR s.project = $3)
            ORDER BY s.project, s.key
            """,
            SEAT_NAMESPACE, SEAT_SCOPE, project, PRESENCE_SCOPE,
        )
        # The R8 predicate as a COUNT, grouped — same clauses as
        # _address_holds_mail so the register and the allocator can never
        # disagree about what parks a name.
        mail_rows = await conn.fetch(
            """
            SELECT user_id, COUNT(*) AS n FROM memories
            WHERE namespace = $1 AND scope = $2
              AND COALESCE((metadata->>'archived')::bool, false) = false
              AND COALESCE(metadata->>'status', $3) = $3
            GROUP BY user_id
            """,
            INBOX_NAMESPACE, INBOX_SCOPE, INBOX_OPEN,
        )
        death_rows = await conn.fetch(
            """
            SELECT project, metadata FROM memories
            WHERE namespace = $1 AND scope = $2
              AND ($3::text IS NULL OR project = $3)
            """,
            SEAT_NAMESPACE, DEATH_SCOPE, project,
        )
        # Person addresses (a human principal's name) are labeled as such
        # instead of "mail-parked" — allocation never considers them.
        human_rows = await conn.fetch(
            "SELECT name FROM principals WHERE type = 'human' AND active",
        )
        # Step 9: known roots (registered OR observed-only — the audit
        # amendment) are reserved; the register serves the same reason the
        # allocator skips on.
        root_rows = await conn.fetch(
            """
            SELECT DISTINCT project FROM memories
            WHERE namespace = $1 AND scope IN ($2, $3)
              AND project IS NOT NULL
            """,
            SEAT_NAMESPACE, PROJECT_REGISTRY_SCOPE, SEAT_SCOPE,
        )

    mail_by_addr = {r["user_id"]: int(r["n"]) for r in mail_rows}
    person_names = {(r["name"] or "").strip().lower() for r in human_rows}
    known_roots = ({r["project"] for r in root_rows}
                   - SEAT_EXEMPT_IDENTITIES)

    def _is_root(addr: str) -> bool:
        return addr.split("@", 1)[0] in known_roots

    def _fixed_reason(addr: str) -> str | None:
        bare = addr.split("@", 1)[0]
        if bare in SEAT_EXEMPT_IDENTITIES:
            return "exempt-role"
        if bare in person_names:
            return "person-address"
        return None

    # Death certs indexed two ways. By session_key is authoritative (the cert
    # names the exact session). By seat name is the SEAT-6 fallback and only
    # trustworthy when the cert carries no key — a keyed cert for a PREVIOUS
    # holder of a reused name must not be pinned on the current one.
    death_by_key: dict[str, dict] = {}
    death_by_seat: dict[str, dict] = {}
    for r in death_rows:
        md = _md(r)
        evidence = {
            "died_at": md.get("died_at"),
            "cause": md.get("cause"),
            "graceful": md.get("graceful"),
            "certified_by": md.get("certified_by"),
        }
        k = md.get("session_key")
        if k:
            prior = death_by_key.get(k)
            if not prior or (md.get("died_at") or "") > (prior.get("died_at") or ""):
                death_by_key[k] = evidence
        elif md.get("seat"):
            s = md["seat"]
            prior = death_by_seat.get(s)
            if not prior or (md.get("died_at") or "") > (prior.get("died_at") or ""):
                death_by_seat[s] = evidence

    def _allocation(age: float | None, mail_n: int,
                    presence_age: float | None,
                    last_used_at, *, lane: bool = False,
                    root: bool = False,
                    fixed_reason: str | None = None) -> dict:
        # Names allocation NEVER touches get their own label instead of
        # "mail-parked" noise: an exempt role (admin — seat_claim returns
        # before its ladder even runs) and a person's address (a human
        # principal's name is not in any candidate space). Rendering those
        # as mail-parked implied the mail was what blocked allocation, on
        # rows where allocation does not apply at all.
        if fixed_reason:
            return {"would_skip": True, "reason": fixed_reason,
                    "grace_expires_at": None}
        # ADDR-REG-1 closed: the ladder is allocation_decision — the SAME
        # function seat_claim consults — so the register and the allocator
        # can no longer drift. This wrapper only maps the register's
        # batch-read columns onto the decision's facts.
        return allocation_decision(
            root=root, lane=lane, age=age, holds_mail=bool(mail_n),
            presence_fresh=(presence_age is not None
                            and presence_age < SEAT_LIVE_SECONDS),
            last_used_at=last_used_at,
        )

    out = []
    seated_addresses = set()
    for r in seat_rows:
        md = _md(r)
        seat = r["key"].removeprefix("seat/")
        seated_addresses.add(seat)
        age = (now - r["last_used_at"]).total_seconds()
        pmd = r["presence_metadata"]
        if isinstance(pmd, str):
            pmd = json.loads(pmd)
        pmd = pmd or {}
        watcher_alive, watcher_seen = _watcher_state(pmd, now)
        presence_age = (
            (now - r["presence_last_used_at"]).total_seconds()
            if r["presence_last_used_at"] else None
        )
        key = md.get("session_key")
        mail_n = mail_by_addr.get(seat, 0)
        death = death_by_key.get(key) if key else None
        if death is None:
            death = death_by_seat.get(seat)
        allocation = _allocation(age, mail_n, presence_age,
                                 r["last_used_at"],
                                 lane=is_reserved_lane(seat, r["project"]),
                                 root=_is_root(seat),
                                 fixed_reason=_fixed_reason(seat))
        # REG-DEATH-1: EVIDENCE OF LIFE AFTER died_at voids a cert — the
        # farewell rule, applied. By-key attachment assumed keys are
        # per-session; a launcher's slot-derived key survives respawns, so
        # a predecessor's cert pinned to the name's CURRENT holder
        # (measured live 2026-08-18 on this project's own PM seat).
        #
        # The rule is AFTER-death life, deliberately NOT bare liveness:
        # a heartbeat can outlive a kill (PICK-REG-1b — the cert is
        # accepted while the row still looks live, and must ride it), so a
        # fresh corpse and a live successor look identical on age alone.
        # What separates them is the clock ORDER: a genuine corpse has no
        # beat, no claim, no presence after its died_at; a reused-key
        # successor always has at least its CLAIM after the predecessor's
        # death. Lock 1's late-cert case falls to the same order: an honest
        # died_at predates the successor's claimed_at, which voids it.
        if death is not None:
            try:
                died = datetime.fromisoformat(death.get("died_at"))
                if died.tzinfo is None:
                    died = died.replace(tzinfo=timezone.utc)
                life_after = r["last_used_at"] > died
                if not life_after and r["presence_last_used_at"]:
                    life_after = r["presence_last_used_at"] > died
                if not life_after and md.get("claimed_at"):
                    try:
                        claimed = datetime.fromisoformat(md["claimed_at"])
                        if claimed.tzinfo is None:
                            claimed = claimed.replace(tzinfo=timezone.utc)
                        life_after = claimed > died
                    except (TypeError, ValueError):
                        pass
                if life_after:
                    death = None  # someone lived here after the "death"
            except (TypeError, ValueError):
                pass  # unparseable died_at: attach as before, facts kept
        out.append({
            "address": seat,
            "entry_type": "seat",
            "project": r["project"],
            "provider": md.get("provider"),
            "host": md.get("host"),
            "hosts_seen": pmd.get("hosts_seen"),
            "session_key": key,
            "session_key_generated": bool(
                key and key.startswith(GENERATED_KEY_PREFIX)
            ),
            "runtime": bool(md.get("runtime")),
            "preferred_seat": md.get("preferred_seat"),
            "claimed_at": md.get("claimed_at"),
            "last_spoke_at": r["last_used_at"].isoformat(),
            "age_seconds": round(age, 1),
            "watcher_alive": watcher_alive,
            "watcher_last_seen": (
                watcher_seen.isoformat() if watcher_seen else None
            ),
            "farewell_at": pmd.get("farewell_at"),
            "death_certified": bool(md.get("death_certified")),
            "death": death,
            "undrained_mail_count": mail_n,
            "allocation": allocation,
        })

    # Names with NO seat row that still hold open mail — R8 parks these
    # (release frees the row, never the mail), and nothing served them until
    # now. '#'-channels are group surfaces, never allocatable names: skip.
    for addr, n in sorted(mail_by_addr.items()):
        if addr in seated_addresses or addr.startswith("#"):
            continue
        if project is not None:
            bare = addr.split("@", 1)[0]
            if bare != project and not bare.startswith(f"{project}-"):
                continue
        out.append({
            "address": addr,
            "entry_type": "mail-only",
            "project": None,
            "provider": None,
            "host": None,
            "hosts_seen": None,
            "session_key": None,
            "session_key_generated": False,
            "runtime": False,
            "preferred_seat": None,
            "claimed_at": None,
            "last_spoke_at": None,
            "age_seconds": None,
            "watcher_alive": None,
            "watcher_last_seen": None,
            "farewell_at": None,
            "death_certified": False,
            "death": death_by_seat.get(addr),
            "undrained_mail_count": n,
            "allocation": _allocation(None, n, None, None,
                                      root=_is_root(addr),
                                      fixed_reason=_fixed_reason(addr)),
        })
    return out


async def project_registry() -> list[dict]:
    """Step 8: every project the store knows — the address tree's roots.

    Two sources, merged: REGISTERED roots (the claim-path census) and
    projects only OBSERVED via seat rows (they predate the registry; they
    register organically on their next claim and are listed meanwhile so
    "every known project" is true from day one, no backfill migration).
    ``dormant`` mirrors the allocator's own clock: no activity within the
    seat-grace window. Facts plus that one policy field, allocator-style.
    """
    now = datetime.now(timezone.utc)
    pool = await get_pool()
    async with pool.acquire() as conn:
        reg_rows = await conn.fetch(
            """
            SELECT project, created_at, COALESCE(last_used_at, created_at) AS la
            FROM memories WHERE namespace = $1 AND scope = $2
            """,
            SEAT_NAMESPACE, PROJECT_REGISTRY_SCOPE,
        )
        seat_rows = await conn.fetch(
            """
            SELECT project, MIN(created_at) AS first_seen,
                   MAX(COALESCE(last_used_at, created_at)) AS la
            FROM memories WHERE namespace = $1 AND scope = $2
              AND project IS NOT NULL
            GROUP BY project
            """,
            SEAT_NAMESPACE, SEAT_SCOPE,
        )
    merged: dict[str, dict] = {}
    for r in seat_rows:
        merged[r["project"]] = {
            "project": r["project"],
            "first_seen": r["first_seen"],
            "last_active": r["la"],
            "registered": False,
        }
    for r in reg_rows:
        e = merged.setdefault(r["project"], {
            "project": r["project"], "first_seen": r["created_at"],
            "last_active": r["la"], "registered": True,
        })
        e["registered"] = True
        if r["created_at"] and (not e["first_seen"]
                                or r["created_at"] < e["first_seen"]):
            e["first_seen"] = r["created_at"]
        if r["la"] and (not e["last_active"] or r["la"] > e["last_active"]):
            e["last_active"] = r["la"]
    out = []
    for e in sorted(merged.values(),
                    key=lambda x: x["last_active"] or x["first_seen"] or now,
                    reverse=True):
        la = e["last_active"]
        out.append({
            "project": e["project"],
            "first_seen": e["first_seen"].isoformat() if e["first_seen"] else None,
            "last_active": la.isoformat() if la else None,
            "registered": e["registered"],
            "dormant": bool(la and (now - la).total_seconds()
                            > PROJECT_DORMANT_SECONDS),
        })
    return out


async def unknown_root_advisories(addrs: list[str]) -> list[str]:
    """Step 8 typo detection, ADDR-2 doctrine: WARN, never reject.

    A destination whose root is no known project, that names no person or
    exempt role, and behind which no seat or presence row exists, is
    probably a typo — the send still succeeds (queued mail is a feature and
    a not-yet-started project is legitimate), but the sender is told that
    nothing has ever listened there, because from the sender's side "never
    existed" and "slow to answer" are otherwise one picture.
    """
    candidates = []
    for a in addrs:
        a = (a or "").strip().lower()
        if not a or a.startswith("#"):
            continue
        candidates.append(a)
    if not candidates:
        return []
    pool = await get_pool()
    async with pool.acquire() as conn:
        projects = await _fetch_known_roots(conn)
        persons = {
            (r["name"] or "").strip().lower() for r in await conn.fetch(
                "SELECT name FROM principals WHERE type = 'human' AND active",
            )
        }
        known_addrs = {
            r["key"].split("/", 1)[1] for r in await conn.fetch(
                """
                SELECT key FROM memories
                WHERE namespace = $1 AND scope IN ($2, $3)
                  AND key = ANY($4::text[])
                """,
                SEAT_NAMESPACE, SEAT_SCOPE, PRESENCE_SCOPE,
                [f"seat/{a}" for a in candidates]
                + [f"presence/{a}" for a in candidates],
            )
        }
    out = []
    for a in candidates:
        bare = a.split("@", 1)[0]
        # A BARE name is a root declaration — O1 makes mailing a channel
        # that has never existed legitimate by design (seeding a project
        # before any session runs), so it must stay silent even when
        # unknown. Only a SUFFIXED name ASSERTS an existing root, and an
        # assertion nothing backs is what a typo looks like.
        if "-" not in bare:
            continue
        if bare in SEAT_EXEMPT_IDENTITIES or bare in persons:
            continue
        if a in known_addrs:
            continue
        if any(bare == p or bare.startswith(p + "-") for p in projects):
            continue
        out.append(
            f"{a}: no registered project roots this address, no session has "
            f"ever held it, and nothing is listening there. Delivered and "
            f"stored — but if this is a typo, it will queue forever. Known "
            f"roots are served by GET /session/projects."
        )
    return out


# Step 13 (climb): how long a LANE must have been silent — across the lane
# row, every incarnation under it, and their presence — before an unhandled
# ask climbs to the project root. Lock 2: dormancy is a WINDOW, not a
# snapshot; a lane empty for two minutes between occupants is succession,
# not abandonment. 3× the live window, same style as the collision window's
# 2.5× — one existing clock multiplied for margin, no new number invented.
CLIMB_LANE_DWELL_SECONDS = SEAT_LIVE_SECONDS * 3


def _parse_tree_node(addr: str, roots: set[str]) -> tuple[str, str, str] | None:
    """Parse an address against the O4 grammar using KNOWN roots.

    Returns (kind, root, parent_address) — kind ∈ {"root", "lane",
    "incarnation"} — or None when no known root grammars the string (a
    role-named seat, a person, a channel: not tree-shaped, never climbed).
    Longest root wins, so 'agentbeast-app-grok' parses under
    'agentbeast-app' when that root exists, not under 'agentbeast'.
    """
    bare = addr.split("@", 1)[0]
    root = max((r for r in roots
                if bare == r or bare.startswith(r + "-")),
               key=len, default=None)
    if root is None:
        return None
    if bare == root:
        return ("root", root, "")
    rest = bare[len(root) + 1:]
    if rest in LANE_PROVIDERS:
        return ("lane", root, root)
    head, dash, ordinal = rest.rpartition("-")
    if head in LANE_PROVIDERS and ordinal.isdigit():
        return ("incarnation", root, f"{root}-{head}")
    return None


async def climb_pass() -> dict:
    """Step 13: unHANDLED asks rise to the nearest living ancestor (O5's
    one exception to depth-is-ephemeral).

    Explicit, idempotent, admin-gated — invoked by the sweep cron, never
    lazy-on-read (climb WRITES). Per pass, per row: at most ONE level up,
    recorded in metadata so history survives. The row keeps its id, so
    threads and in_reply_to answers keep working at the new address, and
    the Step-12 discriminator keeps judging the climbed row.

    Who climbs: open, ASK-class letters whose Step-12 verdict is FALSE.
    True never climbs; UNKNOWN never climbs — the store does not act on a
    guess. Exempt roles never climb (Lock 3). Rows not shaped by the O4
    grammar (role seats, persons, channels) never climb.

    When: an INCARNATION climbs on death evidence for its holder — a cert
    surviving REG-DEATH-1's later-life voiding (Lock 1: a live-holder or
    presence-fresh row voids; a row that spoke after died_at voids), or a
    farewell the register has not voided. A goodbye alone is nothing (T5).
    A LANE climbs when the whole lane subtree has been silent longer than
    CLIMB_LANE_DWELL_SECONDS while the project shows fresh presence
    elsewhere — dormancy-while-ancestor-active; a fully quiet project
    climbs nothing, because quiet is not dead.
    """
    now = datetime.now(timezone.utc)
    climbed: list[dict] = []
    skipped = {"handled": 0, "unknown": 0, "not_tree": 0, "root": 0,
               "exempt": 0, "holder_alive": 0, "no_evidence": 0,
               "lane_active": 0, "project_quiet": 0}
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT key, value, user_id, metadata, created_at FROM memories
            WHERE namespace = $1 AND scope = $2
              AND COALESCE((metadata->>'archived')::bool, false) = false
              AND COALESCE(metadata->>'status', $3) = $3
              AND lower(COALESCE(metadata->>'intent', '')) = ANY($4::text[])
              AND COALESCE(metadata->>'thread_id', '') NOT LIKE 'huddle/%'
            """,
            INBOX_NAMESPACE, INBOX_SCOPE, INBOX_OPEN, list(ASK_INTENTS),
        )
        if not rows:
            return {"climbed": [], "skipped": skipped}
        messages = [_row_to_inbox_message(dict(r)) for r in rows]
        await _mark_handled(conn, messages)
        roots = await _fetch_known_roots(conn)
        register = {e["address"]: e for e in await address_register(None)}
        # Project-level freshness: any register entry under the root that a
        # live session is beating (the ancestor-active half of Lock 2).
        def project_fresh(root: str) -> bool:
            for a, e in register.items():
                node = _parse_tree_node(a, roots)
                if node and node[1] == root and e["allocation"].get(
                        "reason") in ("live-holder", "presence-fresh"):
                    return True
            return False

        def lane_subtree_quiet_since(lane: str, root: str) -> float | None:
            """Newest activity age across the lane and its incarnations;
            None when the subtree has no register entries at all."""
            ages = []
            for a, e in register.items():
                node = _parse_tree_node(a, roots)
                in_subtree = (a == lane) or (node is not None
                                             and node[2] == lane)
                if in_subtree and e.get("age_seconds") is not None:
                    ages.append(e["age_seconds"])
            return min(ages) if ages else None

        for m in messages:
            if m.handled is True:
                skipped["handled"] += 1
                continue
            if m.handled is None:
                skipped["unknown"] += 1
                continue
            bare = m.to.split("@", 1)[0]
            if bare in SEAT_EXEMPT_IDENTITIES:
                skipped["exempt"] += 1
                continue
            node = _parse_tree_node(m.to, roots)
            if node is None:
                skipped["not_tree"] += 1
                continue
            kind, root, parent = node
            if kind == "root":
                skipped["root"] += 1
                continue
            reason = None
            if kind == "incarnation":
                entry = register.get(bare)
                if entry is None:
                    # No register entry at all: nothing has ever held the
                    # name — no death evidence exists either. Hold.
                    skipped["no_evidence"] += 1
                    continue
                if entry["allocation"].get("reason") in (
                        "live-holder", "presence-fresh"):
                    skipped["holder_alive"] += 1
                    continue
                # Death evidence AFTER REG-DEATH-1 voiding (the register
                # already applied it), or an unvoided farewell.
                if entry.get("death"):
                    reason = "holder-death-certified"
                elif entry.get("farewell_at"):
                    reason = "holder-farewell-observed"
                else:
                    skipped["no_evidence"] += 1
                    continue
            else:  # lane
                if not project_fresh(root):
                    skipped["project_quiet"] += 1
                    continue
                newest = lane_subtree_quiet_since(bare, root)
                if newest is not None and newest < CLIMB_LANE_DWELL_SECONDS:
                    skipped["lane_active"] += 1
                    continue
                reason = "lane-dormant-while-project-active"
            await conn.execute(
                """
                UPDATE memories
                SET user_id = $1,
                    metadata = metadata || jsonb_build_object(
                        'climbed_from', $2::text,
                        'climbed_at', $3::text,
                        'climb_reason', $4::text)
                WHERE namespace = $5 AND scope = $6 AND key = $7
                """,
                parent, m.to, now.isoformat(), reason,
                INBOX_NAMESPACE, INBOX_SCOPE, m.id,
            )
            climbed.append({"id": m.id, "from": m.to, "to": parent,
                            "reason": reason})
    return {"climbed": climbed, "skipped": skipped}


# Step 14 (sweep tuning): how old DEEP chatter may grow before its epoch is
# spent — O5's own 72h figure, not a new number. Applies only below a
# project root and never to ask-class mail.
DEEP_CHATTER_EPOCH_SECONDS = 259200  # 72h


async def sweep_pass() -> dict:
    """Step 14: epoch expiry for deep CHATTER (O5 — depth is fragility).

    Chatter only, deep only: open letters that are NOT ask-class, addressed
    below a project root (lane / incarnation per the O4 grammar). Asks are
    NEVER swept — they belong to climb whatever their age; handled asks are
    their parties' to resolve, not the janitor's. Root-level mail is never
    swept (durability lives at the root). huddle/* threads are Band D's.

    Expiry, either condition: the addressed INCARNATION has death evidence
    (the epoch ended — chatter to a dead process is spent by definition), or
    the row is older than DEEP_CHATTER_EPOCH_SECONDS. Mechanism: resolved
    with resolved_by="system:epoch-sweep" — drains from default views,
    retrievable via include_resolved, reversible; never archive's hard hide.
    """
    now = datetime.now(timezone.utc)
    swept: list[dict] = []
    skipped = {"ask": 0, "root": 0, "not_tree": 0, "fresh": 0, "exempt": 0}
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT key, user_id, created_at, metadata FROM memories
            WHERE namespace = $1 AND scope = $2
              AND COALESCE((metadata->>'archived')::bool, false) = false
              AND COALESCE(metadata->>'status', $3) = $3
              AND COALESCE(metadata->>'thread_id', '') NOT LIKE 'huddle/%'
            """,
            INBOX_NAMESPACE, INBOX_SCOPE, INBOX_OPEN,
        )
        if not rows:
            return {"swept": [], "skipped": skipped}
        roots = await _fetch_known_roots(conn)
        register = {e["address"]: e for e in await address_register(None)}
        for r in rows:
            md = _md({"metadata": r["metadata"]})
            if (md.get("intent") or "").strip().lower() in ASK_INTENTS:
                skipped["ask"] += 1
                continue
            to = r["user_id"]
            bare = to.split("@", 1)[0]
            if bare in SEAT_EXEMPT_IDENTITIES:
                skipped["exempt"] += 1
                continue
            node = _parse_tree_node(to, roots)
            if node is None:
                skipped["not_tree"] += 1
                continue
            kind, _root, _parent = node
            if kind == "root":
                skipped["root"] += 1
                continue
            age = (now - r["created_at"]).total_seconds()
            entry = register.get(bare)
            epoch_dead = bool(kind == "incarnation" and entry
                              and entry.get("death"))
            if not epoch_dead and age < DEEP_CHATTER_EPOCH_SECONDS:
                skipped["fresh"] += 1
                continue
            reason = ("incarnation-dead" if epoch_dead
                      else "older-than-epoch")
            await conn.execute(
                """
                UPDATE memories
                SET metadata = COALESCE(metadata, '{}'::jsonb)
                    || jsonb_build_object(
                        'status', $1::text,
                        'resolved_at', $2::text,
                        'resolved_by', 'system:epoch-sweep',
                        'sweep_reason', $3::text)
                WHERE namespace = $4 AND scope = $5 AND key = $6
                """,
                INBOX_RESOLVED, now.isoformat(), reason,
                INBOX_NAMESPACE, INBOX_SCOPE, r["key"],
            )
            swept.append({"id": r["key"], "to": to, "reason": reason})
    return {"swept": swept, "skipped": skipped}
