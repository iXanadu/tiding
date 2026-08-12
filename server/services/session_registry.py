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
        warning = None
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
                        if not await _has_undelivered_mail(conn, seat):
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
                    if await _has_undelivered_mail(conn, dupe):
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
        for seat in seat_candidates(project, provider, preferred):
            # The runtime flag marks a DELIBERATELY-CHOSEN name. It applies
            # only when the granted candidate IS the requested name — a
            # fallback to base/ordinal is an allocation, not a choice.
            meta = _meta(session_key, session_nonce, provider, host,
                         runtime=runtime_seat and seat == preferred)
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
