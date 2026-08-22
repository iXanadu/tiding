"""Watch-claim: one seat, one watch, enforced at the store (v2 design).

Design of record: docs/design/watch-claim.md v2 (adversarially reviewed
2026-08-20 by agentbeast-app-grok-2 — verdict on v1 was "do not build as
written", and this module implements the corrected protocol).

The problem, measured before this existed: the same seat, the same sender,
the identical ask — 50 minutes to answer without a watcher, under 2 minutes
with one. Watcher arming was an agent-performed startup ritual, and one day
(2026-08-20) produced its complete failure catalog: never armed,
believed-armed-never-ran (a ps check matched a NEIGHBOR's watcher),
armed-then-died, armed-twice (double wakes), and armed-but-listening-on-the-
wrong-addresses (a seat-addressed DM could never wake it while every
liveness probe passed).

What this module enforces:
- A watch on a seat is a single-holder claim, granted by the store.
- The claim lives by beats and dies by silence (EXPIRY); recovery costs no
  model turns — a dead holder's slot is simply taken by the next arrival.
- Beat and steal are ONE compare-and-swap statement on a RANDOM nonce.
  The dual-holder race the reviewer found in v1 (a stalled holder's
  in-flight beat refreshing a row a successor just stole) cannot happen:
  a beat that does not match the current nonce updates nothing and returns
  `displaced`. A pid is never a nonce — pid reuse inside the expiry window
  would reincarnate a ghost.
- The claim records the holder's PROJECT_DIR and LISTEN_SET, and a claim
  whose listen set omits the seat it names is refused as `partial` — a
  partial watch is not a watch. This is F10, found in the wild: a bare
  watcher held "coverage" for a seat it was not listening for, printed an
  estate survey LISTING that seat, and said nothing.

What this module deliberately does NOT do:
- It never gates DELIVERY. AB's user-turn injection (D2) is delivery, not
  sensing, and proceeds regardless of who holds the watch. A mute holder
  must not lock out a working deliverer (review kill K2); delivery-liveness
  displacement is step 5 of the build order, layered on top.
- It never blocks a watcher from RUNNING. When this API is unreachable, a
  manually launched watcher runs UNCLAIMED and loudly UNHELD (review kill
  K3 — the repair crew hears each other while the store is sick). The
  register simply never shows an unheld seat as covered.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone

from server.db import get_pool
from server.services.memory_service import INBOX_NAMESPACE

logger = logging.getLogger(__name__)

WATCH_NAMESPACE = INBOX_NAMESPACE
WATCH_SCOPE = "watch"
WATCH_USER_ID = "global"

# 3 missed ~45s beats. Reviewer-ratified bounds: floor 90 (a hung server
# should not displace everyone), ceiling 180 (a human-noticeable silence).
WATCH_EXPIRY_SECONDS = 150

# K2 / delivery-liveness: how long mail may sit unfetched by a BEATING
# holder before that holder is displaceable. Generous on purpose — this
# branch exists for the mute-monopoly case, not for racing a slow poll.
MUTE_GRACE_SECONDS = 600
# And how far back the mute check looks. Bounds the query and stops ancient
# never-fetched mail (predecessor estates) from making every holder
# permanently displaceable on day one.
MUTE_LOOKBACK_SECONDS = 6 * 3600


def mint_nonce() -> str:
    """Random, never derived from pid — pid reuse inside the expiry window
    reincarnates a dead holder's identity (same ghost class as seat nonces)."""
    return secrets.token_hex(16)


def _row_meta(seat: str, nonce: str, armed_by: str, project_dir: str,
              listen_set: list[str], host: str | None,
              seat_nonce: str | None = None) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "kind": "watch",
        "seat": seat,
        "nonce": nonce,
        "armed_by": armed_by,          # bridge | ab | agent — provenance, never authority
        "project_dir": project_dir,    # F10/P2: the tree this watcher actually polls for
        "listen_set": listen_set,      # F10/P2: what it actually wakes on
        "host": host,
        # WATCH-CLAIM-4(b): the seat register's per-process nonce of the
        # session this watcher serves (the bridge hands it to the watcher it
        # spawns). Lets the claim FOLLOW THE SEAT — see watch_claim.
        "seat_nonce": seat_nonce,
        "claimed_at": now,
        "last_beat": now,
    }


async def watch_claim(
    seat: str,
    nonce: str,
    armed_by: str,
    project_dir: str,
    listen_set: list[str],
    host: str | None = None,
    seat_nonce: str | None = None,
) -> dict:
    """Claim the watch for ``seat``. Returns a verdict dict, never raises
    for protocol outcomes.

    Verdicts:
      granted            — you hold the watch; start polling.
      held               — a live holder exists; RE-CLAIM ON A TIMER, do not
                           exit forever (v1's exit-forever meant mail died
                           with whichever process claimed first — kill K1).
      partial-refused    — your listen_set does not contain the seat you are
                           claiming for. You would hold coverage for an
                           address you cannot hear (F10). Fix the listen set;
                           do not retry as-is.

    WATCH-CLAIM-4(b) — THE CLAIM FOLLOWS THE SEAT. A claim is keyed by seat
    NAME, and names are re-granted: a successor on a re-granted ordinal, or
    the same session's bridge restarted, inherited the corpse's claim for as
    long as the corpse kept beating (≤ EXPIRY, ~150s of deafness per
    restart; measured 2026-08-21 14:01Z). The seat REGISTER is the authority
    on who occupies a name: its row carries the occupant's per-process
    ``session_nonce`` (SEAT-9 newest-wins refreshes it on every re-claim).
    So a claimant that presents the seat's CURRENT nonce is the occupant's
    watcher, and any incumbent watch that does not carry that nonce serves a
    process the register no longer seats — stealable at once, beats or no
    beats. A claimant whose nonce does not match the register (older
    watcher, hand-launched, AB-armed) falls through to the expiry / mute
    rules unchanged, so nothing that worked before loses its claim here.
    """
    seat = (seat or "").strip().lower()
    if not seat:
        return {"verdict": "partial-refused", "reason": "empty seat"}
    normalized = [a.strip().lower() for a in (listen_set or []) if a and a.strip()]
    bare = [a.split("@", 1)[0] for a in normalized]
    if seat not in normalized and seat not in bare:
        return {
            "verdict": "partial-refused",
            "reason": (
                f"listen_set does not include the seat '{seat}' — a partial "
                "watch is not a watch (F10: a seat-addressed DM would never "
                "wake this watcher while the register showed it covered)"
            ),
            "listen_set": normalized,
        }

    seat_nonce = (seat_nonce or "").strip() or None
    meta = _row_meta(seat, nonce, armed_by, project_dir, normalized, host,
                     seat_nonce)
    key = f"watch/{seat}"
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Try the free-slot insert first; the 5-tuple unique key settles
        # same-instant races (one INSERT wins, the loser falls through).
        inserted = await conn.fetchval(
            """
            INSERT INTO memories (namespace, key, value, scope, user_id,
                                  project, tags, tags_search, metadata)
            VALUES ($1, $2, 'watch', $3, $4, '', '', '', $5::jsonb)
            ON CONFLICT (namespace, key, scope, user_id, project) DO NOTHING
            RETURNING 1
            """,
            WATCH_NAMESPACE, key, WATCH_SCOPE, WATCH_USER_ID, json.dumps(meta),
        )
        if inserted:
            return {"verdict": "granted", "seat": seat, "expiry_seconds": WATCH_EXPIRY_SECONDS}

        # Occupied. Steal iff expired OR delivery-dead — ONE CAS statement,
        # so a concurrent steal and a stalled holder's late beat cannot both
        # win (P1).
        #
        # The second branch is K2, the review's monopoly kill: a holder that
        # BEATS but does not FETCH looked exactly like coverage tonight
        # (huddle-fast, DM-deaf) and under a naive exclusive claim it would
        # LOCK OUT the working watcher. Here it is displaceable once mail
        # addressed to its own recorded listen_set has been waiting longer
        # than MUTE_GRACE while its fetched_through never advanced past it.
        # A holder that never reports fetched_through at all gets the benefit
        # of the doubt only until mail outwaits the grace against its
        # claimed_at.
        # Read the incumbent's watermark BEFORE the CAS. A stale read here is
        # harmless — it only widens the successor's catch-up window; the CAS
        # below still decides who wins.
        prior = await conn.fetchrow(
            "SELECT metadata FROM memories WHERE namespace=$1 AND key=$2 "
            "AND scope=$3 AND user_id=$4",
            WATCH_NAMESPACE, key, WATCH_SCOPE, WATCH_USER_ID,
        )
        prior_md = prior["metadata"] if prior else None
        if isinstance(prior_md, str):
            prior_md = json.loads(prior_md)
        prior_md = prior_md or {}
        stole = await conn.fetchval(
            """
            UPDATE memories w
            SET metadata = $5::jsonb, last_used_at = NOW()
            WHERE w.namespace = $1 AND w.key = $2 AND w.scope = $3
              AND w.user_id = $4
              AND (
                (w.metadata->>'last_beat')::timestamptz
                    < NOW() - make_interval(secs => $6)
                -- WATCH-CLAIM-4(b) claim-follows-seat: the claimant IS the
                -- register's current occupant (its nonce matches the seat
                -- row) and the incumbent watch does not carry that nonce.
                OR ($9::text IS NOT NULL
                    AND w.metadata->>'seat_nonce' IS DISTINCT FROM $9::text
                    AND EXISTS (
                      SELECT 1 FROM memories s
                      WHERE s.namespace = $1 AND s.scope = 'seat'
                        AND s.user_id = 'global'
                        AND s.key = 'seat/' || $10::text
                        AND s.metadata->>'session_nonce' = $9::text))
                OR EXISTS (
                  SELECT 1 FROM memories m
                  WHERE m.namespace = $1 AND m.scope = 'inbox'
                    AND m.user_id = ANY(
                        SELECT jsonb_array_elements_text(w.metadata->'listen_set'))
                    AND COALESCE((m.metadata->>'archived')::bool, false) = false
                    AND m.created_at < NOW() - make_interval(secs => $7)
                    AND m.created_at > GREATEST(
                        COALESCE((w.metadata->>'fetched_through')::timestamptz,
                                 (w.metadata->>'claimed_at')::timestamptz),
                        NOW() - make_interval(secs => $8))
                )
              )
            RETURNING 1
            """,
            WATCH_NAMESPACE, key, WATCH_SCOPE, WATCH_USER_ID, json.dumps(meta),
            WATCH_EXPIRY_SECONDS, MUTE_GRACE_SECONDS, MUTE_LOOKBACK_SECONDS,
            seat_nonce, seat,
        )
        if stole:
            return {
                "verdict": "granted", "seat": seat, "stolen": True,
                # Why the incumbent lost, for the log line: the register
                # says the claimant is the occupant and the incumbent was
                # not (claim-follows-seat), vs. it simply went silent/mute.
                "displaced_reason": (
                    "claim-follows-seat"
                    if seat_nonce and prior_md.get("seat_nonce") != seat_nonce
                    else "expired-or-mute"),
                "expiry_seconds": WATCH_EXPIRY_SECONDS,
                # P4: gap mail must never depend on a side path reaching
                # back. The successor catches up from what the corpse
                # PROVABLY delivered (its fetched_through), falling back to
                # when the corpse claimed — mail after that point may never
                # have been emitted by anyone.
                "catch_up_after": (prior_md.get("fetched_through")
                                   or prior_md.get("claimed_at")),
            }

        row = await conn.fetchrow(
            """
            SELECT metadata,
                   EXTRACT(EPOCH FROM (
                       (metadata->>'last_beat')::timestamptz
                       + make_interval(secs => $5) - NOW()
                   )) AS stealable_in
            FROM memories
            WHERE namespace=$1 AND key=$2 AND scope=$3 AND user_id=$4
            """,
            WATCH_NAMESPACE, key, WATCH_SCOPE, WATCH_USER_ID,
            WATCH_EXPIRY_SECONDS,
        )
        held = row["metadata"] if row else None
        if isinstance(held, str):
            held = json.loads(held)
        held = held or {}
        # WATCH-CLAIM-4(a): retry when the holder's claim becomes STEALABLE,
        # not a flat EXPIRY. 2026-08-21 a successor on a re-granted seat
        # claimed 4s before its predecessor's beat aged out, was told "150",
        # and sat uncovered 3.5 minutes over a claim that expired 8s later
        # (while watch_status already read `expired` for the same row).
        # Floor a few seconds so a just-beaten holder is not hammered;
        # ceiling EXPIRY so a future-dated beat cannot park a watcher forever.
        stealable_in = row["stealable_in"] if row else None
        try:
            remaining = float(stealable_in) if stealable_in is not None else None
        except (TypeError, ValueError):
            remaining = None
        if remaining is None:
            retry_after = WATCH_EXPIRY_SECONDS
        else:
            retry_after = max(5.0, min(float(WATCH_EXPIRY_SECONDS), remaining + 2.0))
        return {
            "verdict": "held",
            "seat": seat,
            "holder_armed_by": held.get("armed_by"),
            "holder_since": held.get("claimed_at"),
            "holder_last_beat": held.get("last_beat"),
            # The caller's contract: retry on a timer. Stated in the response
            # so no client has to remember it from prose.
            "retry_after_seconds": round(retry_after, 1),
        }


async def watch_beat(seat: str, nonce: str,
                     fetched_through: str | None = None) -> dict:
    """One beat. Returns ``holder`` or ``displaced`` — never ambiguity.

    The beat is a CAS on the nonce: if this process no longer holds the
    watch, the UPDATE matches nothing and the verdict says so. The caller's
    contract (stated here because callers read docstrings, not designs):
    on `displaced` — exit, then re-claim on a timer if you still want the
    watch. On a LOST RESPONSE (timeout, 5xx) — treat as holder-unknown and
    STOP EMITTING until a beat succeeds; emitting while unsure is how two
    watchers deliver the same mail twice.
    """
    seat = (seat or "").strip().lower()
    key = f"watch/{seat}"
    pool = await get_pool()
    async with pool.acquire() as conn:
        beat = await conn.fetchval(
            """
            UPDATE memories
            SET metadata = metadata
                    || jsonb_build_object('last_beat', NOW()::text)
                    || CASE WHEN $6::text IS NULL THEN '{}'::jsonb
                            ELSE jsonb_build_object('fetched_through', $6::text)
                       END,
                last_used_at = NOW()
            WHERE namespace = $1 AND key = $2 AND scope = $3 AND user_id = $4
              AND metadata->>'nonce' = $5
            RETURNING 1
            """,
            WATCH_NAMESPACE, key, WATCH_SCOPE, WATCH_USER_ID, nonce,
            fetched_through,
        )
    if beat:
        return {"verdict": "holder", "seat": seat}
    return {"verdict": "displaced", "seat": seat}


async def watch_release(seat: str, nonce: str) -> dict:
    """Graceful exit: free the slot iff we still hold it (same CAS rule —
    releasing a watch someone else now holds would free THEIR claim)."""
    seat = (seat or "").strip().lower()
    key = f"watch/{seat}"
    pool = await get_pool()
    async with pool.acquire() as conn:
        released = await conn.fetchval(
            """
            DELETE FROM memories
            WHERE namespace = $1 AND key = $2 AND scope = $3 AND user_id = $4
              AND metadata->>'nonce' = $5
            RETURNING 1
            """,
            WATCH_NAMESPACE, key, WATCH_SCOPE, WATCH_USER_ID, nonce,
        )
    return {"verdict": "released" if released else "not-holder", "seat": seat}


async def watch_status(seat: str) -> dict:
    """What the register can honestly say about a seat's watch.

    Three-valued on purpose, mirroring _watcher_state's discipline:
      covered   — live holder, beating inside the window
      expired   — a holder exists but has gone silent past EXPIRY
      unheld    — no claim at all. NEVER rendered as "dead"; a session may
                  be running UNHELD legitimately (store was unreachable at
                  arm time — kill K3), it is just not COVERED.
    """
    seat = (seat or "").strip().lower()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT metadata,
                   (metadata->>'last_beat')::timestamptz
                       >= NOW() - make_interval(secs => $5) AS fresh
            FROM memories
            WHERE namespace=$1 AND key=$2 AND scope=$3 AND user_id=$4
            """,
            WATCH_NAMESPACE, f"watch/{seat}", WATCH_SCOPE, WATCH_USER_ID,
            WATCH_EXPIRY_SECONDS,
        )
    if not row:
        return {"state": "unheld", "seat": seat}
    md = row["metadata"]
    if isinstance(md, str):
        md = json.loads(md)
    return {
        "state": "covered" if row["fresh"] else "expired",
        "seat": seat,
        "armed_by": md.get("armed_by"),
        "listen_set": md.get("listen_set"),
        "project_dir": md.get("project_dir"),
        "last_beat": md.get("last_beat"),
    }
