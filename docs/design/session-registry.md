# Session registry: allocating addresses instead of computing them

Status: **Phase 1 implemented and committed, NOT yet deployed** (2026-07-24).
Extends the seat work shipped 2026-07-21..23 (SEAT-1, SEAT-2, seat-collision
detection). Phase 2 (roster lifecycle) remains open. There is no role-alias
layer — see "Roles are not a third layer" below.

## The one-sentence problem

Every session **computes** its own address from data that is not unique, so
`docs/messaging.md`'s stated invariant — *"two agents never share an identity;
they share subscriptions"* — is documented but not enforced, and the system
detects violations after the fact instead of preventing them.

## What is actually true today (verified, not recalled)

| Mechanism | Where | Uniqueness |
|---|---|---|
| Project name → address | `identity.derive_project_name` | per **folder** |
| Seat `<project>-<provider>` | AB `engram_env.seat_for` | per **(folder, provider)** |
| `ENGRAM_SESSION_KEY` | AB `engram_env.session_key` | per **session** ✅ |
| tmux name `ab-engram-2` | AB `_unique_tmux_name` | per **session** ✅ |

The uniqueness already exists — `_unique_tmux_name()` allocates `ab-engram`,
`ab-engram-2`, `ab-engram-3` — and is fed into `ENGRAM_SESSION_KEY`. It is then
**discarded at the seat**, which is computed from `(project, provider)`. Three
Claude sessions in one folder therefore all seat as `engram-claude`: shared
ack-state, mutual self-echo drop, unable to wake each other. This is exactly the
orchestrator/tester/implementer case.

Two further facts decide the design:

1. **A launcher can only dedupe what it launched.** A hand-started terminal
   session is invisible to AgentBeast, so AB's `-2` logic cannot see it and both
   sessions take the same seat. engram is the only party that sees every session
   regardless of who spawned it. **Allocation has to be server-side.**
2. **Nothing ever releases a presence row.** Live roster today: 10 entries, 9
   stale, oldest 2.8 days, every one still `state="running"`. Any registry needs
   a lifecycle or it becomes landfill.

## Requirements

| | |
|---|---|
| R1 | Unique by construction — N sessions, N addresses, no human step |
| R2 | No launcher dependency — hand-launched sessions self-allocate |
| R3 | Stable — the address must not change under a running session |
| R4 | Bridge and watcher always resolve the **same** address |
| R5 | Backward compatible — existing launchers and sessions keep working |
| R6 | Roles addressable — ordinals carry no meaning |
| R7 | Bounded growth — seats reclaimable, ordinals stay small |
| R8 | **No misdelivery** — reclaim must never route A's mail to B |

R8 outranks R7. Accreting numbers is untidy; delivering a message to the wrong
agent is a correctness failure.

## Design: two layers (and why not three)

```
GROUP   engram                every session in the project   (unchanged)
SEAT    engram-claude-2       exactly one session            (NEW: allocated)
```

**GROUP** is Rob's "global listening" and already works — `compute_identity`
puts the bare project name in every listen_set, seat or no seat.

**SEAT** guarantees uniqueness *without needing to know anything*, so it can be
assigned at spawn. It is provider-discriminated (`-claude`, `-grok`) and
ordinal-suffixed when a peer already holds the base (`-2`), and that is the
*whole* address.

### Roles are not a third layer (Rob, 2026-07-24)

An earlier draft added `ROLE` (`engram-orchestrator`) as an optional third
address. That was wrong, for reasons that also explain why a role can't be a
seat:

- **A role is not unique.** You might ask *both* grok and claude to test.
  `engram-tester` would then name two sessions — the exact two-bodies-one-
  identity collision seats exist to kill, reintroduced through a new door.
- **A role is not provider-stable.** The alias carried no provider, so it
  couldn't even distinguish the grok tester from the claude tester.
- **A role is assigned late, to a chosen seat.** The owner picks specific seats
  in a huddle ("you — `engram-claude-2` — test") and assigns the role there.
  The binding lives in the **huddle/orchestration layer (AgentBeast)**, which
  already knows role→seat; the addressing layer never needs it.

Roles are the vocabulary for *why* you want several agents in a folder. That
vocabulary belongs in the huddle, not in a listen address. Addressing stays two
layers; the seat is pure plumbing.

## The claim protocol

```
POST /session/claim
  { session_key, project, provider, host, preferred_seat? }
→ { seat, listen_set, is_new, reclaimed_from?, warning? }
```

Idempotent on `session_key`; called by the bridge at startup and refreshed on
the existing heartbeat.

Resolution order:

1. **My own key already holds a seat** → refresh, return it. *(R3)*
2. **Preferred seat is free** → take it. *(R5 — AB's current behaviour is the happy path)*
3. **Preferred seat is QUIET, same `(project, provider, host)`, no unread mail**
   → take it over. *(Restarted harness lands back on its own seat instead of drifting to `-2`.)*
4. **Next free ordinal** — lowest first. *(R7)*
5. **Next reclaimable ordinal.**
6. Cap (64) → explicit error, never a silent duplicate.

### Why this is atomic without locks

The existing constraint

```sql
UNIQUE NULLS NOT DISTINCT (namespace, key, scope, user_id, project)
```

makes `INSERT ... ON CONFLICT DO NOTHING` a correct compare-and-swap. Two
sessions racing for `engram-claude`: the index serialises them, one inserts, the
loser gets 0 rows and advances to `engram-claude-2`. Takeover is a conditional
`UPDATE ... WHERE last_used_at < now() - grace`, which returns 0 rows if someone
refreshed meanwhile. No advisory locks, no transaction gymnastics, no new table
— seat rows are `scope='seat'` (a new reserved scope alongside `inbox` and
`presence`).

## Session key without a launcher (R2)

Precedence:

1. `ENGRAM_SESSION_KEY` — launcher-injected. AB's tmux name is ideal: unique per
   box, survives respawn, never pid-derived.
2. **Auto-derived: `auto-<host>-<harness_pid>-<harness_start_epoch>`.**
   For the bridge, `harness_pid = os.getppid()` — reliable *by construction*,
   because an MCP stdio server is spawned as a direct child of the harness.
   The start epoch defeats PID reuse.
3. Unresolvable → random per-process key. Degrades to exactly today's behaviour.

Verified on the live tree: bridge `4830 → ppid 4813 = claude`.

> Do **not** walk to `ppid == 1`. That reaches the tmux *server* (PID 4812
> here), which is shared by every tmux session on the box — it would hand all
> sessions one key, the precise opposite of what is needed.

## Making the watcher agree (R4)

Three mechanisms, deliberately redundant, because a bridge seated correctly with
a watcher listening elsewhere is *worse* than no seat at all — the roster shows
the session correctly seated while it silently never wakes.

**(a) The seat file — already shipped (SEAT-2).** The bridge writes
`~/.local/state/engram/seats/<session_key>.seat`; `inbox_wait.py` re-resolves
identity **every poll** and follows changes with no restart. Works today
whenever `ENGRAM_SESSION_KEY` is set.

**(b) Ancestor-walk discovery — NEW, closes the hand-launched gap.** The watcher
walks its own ancestor PID chain and, for each ancestor, tries
`auto-<host>-<pid>-<start>.seat`. The bridge wrote exactly one such file, keyed
on the harness PID — which is on the watcher's ancestor chain. Nearest hit wins
(correct under nested harnesses).

Verified on the live tree: watcher `6177 → 6175 (zsh) → 4813 (claude)`, and
4813 is the bridge's parent. **The two chains provably intersect at the
harness.** That is a structural property of how the harness spawns both, not a
coincidence of one snapshot.

**(c) Explicit belt.** The claim result prints the exact arm command including
`ENGRAM_SESSION_KEY=`, so a session following `/startup` gets it stated.

### The ordering hazard, and why it is benign

The watcher may be armed *before* the bridge claims. It then starts on the
project group address and picks up its seat on a later poll. It never goes deaf
— it goes from coarse to precise. That is the correct direction to fail.

## Reclamation (R7, R8)

| State | Condition | Reclaimable |
|---|---|---|
| LIVE | heartbeat < 10 min | no |
| QUIET | < grace (default 2 h, tunable) | only per rule 3 above |
| RECLAIMABLE | > grace | yes |

Two hard guards:

- **Never reclaim a seat with unread mail addressed to it.** One `inbox_list`
  with `limit=1`. This is what makes R8 hold: mail in flight for a dead session
  is *preserved for its successor*, never handed to a stranger.
- **Explicit release** (`POST /session/release`, or the existing `done` state)
  frees a seat immediately — the clean path, always preferred over expiry.

**Losing a seat is never silent.** A session whose seat was reassigned discovers
it at its next claim: its `session_key` no longer holds the row, so it re-claims
and gets `is_new: true` plus a warning, and its watcher follows via the seat
file. Compare this to today, where the failure mode is quiet by construction.

**Roster lifecycle** (the same disease, smaller): mark entries past grace
`presumed-dead` rather than showing a 2.8-day-old row as `running`, and let the
existing cleanup task drop presence rows past a retention horizon.

## Who does what

| Party | Change |
|---|---|
| **engram** | registry rows, claim/release/seats endpoints, bridge claim-on-startup, watcher ancestor-walk, roster lifecycle |
| **AgentBeast** | *nothing mandatory* for addressing. Keeps injecting `SESSION_KEY`/`PROVIDER`/`CHANNELS`. Its `INBOX_IDENTITY` becomes a **preference** the server grants or supersedes. One UI change: **show the ordinal** (`-2`) in the session picker and read the granted seat via `/session/seats?session_key=…` instead of recomputing it |
| **Rob** | nothing — hand-launched sessions self-allocate |

## Rollout — three independently shippable, revertible phases

- **Phase 1** (shipped) — bridge claims at startup; env seat becomes a
  preference; watcher ancestor-walk; launcher readback; the `derive_listen_set`
  guidance fix (ADDR-1). *This is the phase that closes Rob's three-Claude case.*
- **Phase 2** — roster lifecycle (`presumed-dead`, reaping).

Phase 1 is the whole payload. Phase 2 is hygiene.

## Four holes found by attacking the design, and how each closes

**1. A non-unique `session_key` would silently recreate the bug.** Step 1 of the
claim returns the existing seat for a matching key — so two processes sharing a
key (a hand-exported `ENGRAM_SESSION_KEY`, a launcher bug) would both be handed
the *same* seat, which is precisely the failure being fixed, now blessed by the
server.

*Close it:* record the per-process `session_nonce` (already generated by the
bridge for collision detection) on the seat row alongside the key. Then
**`session_key` means continuity, `(key, nonce)` means identity**:

| Claim arrives | Holder state | Result |
|---|---|---|
| same key, same nonce | any | refresh — normal heartbeat |
| same key, *different* nonce | **LIVE** | grant next ordinal + `warning: session_key is not unique` |
| same key, different nonce | not live | same seat — a genuine restart *(R3)* |

The restart case (what R3 needs) and the duplicate-key case are distinguished by
holder liveness, so hardening R3 does not reintroduce the collision.

**2. `admin` must stay shared.** `SEAT_EXEMPT_IDENTITIES = {"admin"}` exists
because maintenance sessions across boxes deliberately share one role identity.
The allocator must honour the same exemption: **exempt identities are never
allocated and never collide-flagged.** Deliberate role-sharing is a feature; the
registry must not "fix" it.

**3. The server can be down when a session starts.** A claim that fails must
never block or unseat a session. *Close it:* on any claim error the bridge keeps
today's env/cfg-derived seat and retries on the next heartbeat. The registry is
an improvement over the fallback, never a dependency of it. A session with an
unreachable engram behaves exactly as it does today.

**4. Seats are fleet-wide, not per-box — state it out loud.** The seat row is
keyed on `project`, and one engram serves the whole fleet, so a session in
`engram` on macmini and another on dbone cannot both hold `engram-claude`; the
second gets `-2`. This is *desirable* — the loose address `engram-claude` is
already unqualified and is in both listen_sets, so today those two sessions
silently share an address. Fleet-wide allocation makes every seat globally
unambiguous. Host is recorded on the row for diagnosis, not for scoping.

## Verification — what was proven, not asserted

Both load-bearing mechanisms were prototyped and run before this document was
finished. Neither rests on reasoning alone.

**1. Ancestor discovery (R2, R4b).** A probe run from a plain Bash tool call —
spawned exactly the way the inbox watcher is — walked its own ancestor chain and
cross-checked it against the live bridge process:

```
pid=11584 (python)  ppid=11582
pid=11582 (zsh)     ppid=4813
pid=4813  (claude)  ppid=4812     <- bridge 4830's parent
pid=4812  (tmux)    ppid=1        <- SHARED by all tmux sessions; never a key
✅ INTERSECTION at pid [4813] — the watcher's ancestor chain contains the
   bridge's parent.
```

The chains provably meet at the harness. That is structural (the harness spawns
both), not a lucky snapshot.

**2. Race-safe allocation with no locks (R1, R3, R7).** Twelve sessions claimed
`raceproj-claude` simultaneously against Postgres (`engram_test`, never prod):

```
granted=12  distinct=12  failed=0
PASS  every session got a distinct seat
PASS  ordinals dense + lowest-first (no gaps)
PASS  re-claim with the same session_key returned identical seats (R3 —
      a bridge restart burns no ordinal)
PASS  a newcomer after the race got -13, never a duplicate
```

Incidental finding that improves the design: `embedding` is nullable, so seat
rows carry **no embedding at all** — a pure registry write, unlike presence
rows, which embed on first insert. Seat claims are therefore cheap enough to
refresh on the existing heartbeat.

## Risks and open trade-offs — stated, not buried

1. **Ordinal drift across a long gap.** A session quiet past grace can return as
   `-3` while peers hold `-2`. Mitigated by rule 3 (same-slot takeover) and the
   unread-mail guard; not eliminated. Accepted — the alternative is unbounded
   growth. Grace is the tuning knob: longer favours stability, shorter favours
   tight numbering.
2. **Broadcast stampede.** All N sessions listen on `<project>`, so one
   project-addressed `action` wakes all N and all may act. This is an
   **orchestration** problem (AgentBeast's domain per
   `decision/claude-session-manager-direction`), not an addressing one — but the
   registry makes it *more likely* by making 3-session projects easy. Flagged
   deliberately; not solved here.
3. **Ancestor-walk is POSIX-specific** (`ps`). macOS and Linux are fine; anything
   else degrades to (a) and (c).
4. **One extra startup round-trip.** Piggybacks on the existing heartbeat.

## Bug found while researching this (separate, small)

`server/services/inbox_guidance.py::derive_listen_set` reconstructs a listen_set
by string-splitting `reader_identity`, and its docstring claims it "mirrors the
MCP bridge's `compute_identity()`". Since seats shipped it **cannot** — from
`engram-claude@macmini` it is unable to recover either the project group address
(`engram`) or channel subscriptions (`#devagents`). Live proof, same session,
same call sequence:

```
memory_inbox → ['engram-claude', 'engram', 'machine:macmini', 'engram-claude@macmini', '#devagents']
memory_send  → ['engram-claude',            'machine:macmini', 'engram-claude@macmini']
```

Guidance text only — **delivery is unaffected**, since routing uses the real
listen_set the bridge sends. But it under-reports to an agent at the exact
moment it is deciding how to address peers: a session reading it would
reasonably conclude it is not in `#devagents` and not reachable at its project
address. Same class as the hardcoded `provider` that misled agentbeast-app into
reporting a constant to Rob as fact — a field that cannot vary is worse than a
missing one.

Fix: pass the real listen_set from the client into `send_guidance` (the bridge
already computes it and currently discards it with `_` at
`server.py:935`). Tracked as **ADDR-1**.
