# Immortal Addresses — mail is never sent to a mortal thing

**Status: DRAFT v3 — REVIEW-ACCEPTED, awaiting owner ratification. Not
scheduled, nothing built.** Author: engram-claude-3, 2026-08-14. Reviewer:
agentbeast-grok-4. Cycle: v1 attacked (three kills, all accepted), v2
re-reviewed ("doctrine + architecture accepted; four conditions before
schedule"), v3 writes the four conditions in. Reviewer has committed to no
further full attack unless a new kill appears. Deltas marked **[v2]**/**[v3]**.

## The rule under test

> Mail is only ever sent to **immortal addresses**. Mortal things — sessions —
> are **provenance** (who a message is *from*) and **occupancy** (who is on a
> lane right now), never the default destination.

Immortal: the project channel (`softphone`), declared sub-team groups
(`beastchat-app`), **lanes** (`softphone-grok`), machines, humans, `#channels`.
Mortal: a session's occupant identity (`agentbeast-grok-4`).

**[v2] Seat DMs remain first-class, not vestigial.** Two legitimate uses
survive review: (1) targeting one specific occupant when a lane has two
(build vs audit in one folder — the founding case); (2) mail that must
die with its recipient (one-shot tokens, parked confirmations — content the
next occupant must NOT read). The doctrine changes the *default* and the
ergonomics, not the legality.

## Scope — what this fixes and what it does not **[v2: overclaim retracted]**

Fixes the measured mortality class: mail stranding at dead seats (133
never-read at `softphone-claude` vs 1,981 read at the same address — the lane
worked, the mortality failed), R8 name-locks (`agentbeast-grok-2/-3/-6`),
ordinal creep, successor-walks-past-backlog.

Explicitly does NOT fix, and must not be sold as fixing: RELAY-1
(envelope-vs-body authorship), IDENT-1 (spawn env overriding repo-declared
identity — owner's open decision), cursor/codex env-propagation quirks,
huddle channel-vs-private bleed. Those stay open on their own terms.

## Grounding (owner, 2026-08-14)

The project dictates the channel; channel mail is never drained-as-
undeliverable; it queues for future occupants; every session listens at its
channel level(s) AND its occupant level. Already true; becomes the primary
delivery model.

## Architecture

Three address strata, all visible in one flat send-string space (ADDR-3's
`kind` field makes them legible to pickers):

1. **Channels** (project, declared groups, machine, #channels) — immortal,
   many listeners, broadcast semantics. Unchanged.
2. **Lanes** (`<project>-<provider>`, or repo-declared) — immortal, "whoever
   is/next is the <provider> on <project>". Mail queues across occupancy
   gaps. THE default for directed sends.
3. **Occupants** (today's seats) — mortal, exactly-one-session, allocated by
   the registry exactly as today. `From:` identity, presence, self-echo,
   collision detection, huddle pinning, die-with-recipient mail.

### **[v2] The lane/seat string split — the migration hazard, named**

Reviewer's kill: today the lane string IS a seat string. AB already injects
`ENGRAM_INBOX_IDENTITY=<project>-<provider>` every spawn (engram_env.py:239);
engram treats it as an allocable seat, the corpse holds it, R8 locks it, the
newcomer gets an ordinal. Declaring a GROUP with the same string while the
corpse seat exists is a name collision, not a migration.

Resolution (one flat namespace kept deliberately — senders must not need to
know an address's kind to use it):
- **Lane strings become reserved**: once a lane exists (implicit
  `<project>-<provider>` or declared), the seat allocator REFUSES to mint a
  seat with that exact string. Occupant identities are always
  distinguishable from the lane (ordinal or otherwise).
- **One-time corpse drain precedes reservation** per project: existing
  locked seats matching lane strings are drained (manual prototype already
  run on `softphone-claude`, marker `dead-address-drain`) and released.
- The injected `ENGRAM_INBOX_IDENTITY=<lane>` is REINTERPRETED by the bridge:
  it names the lane to listen on; the occupant identity is allocated
  separately by the registry. (AB reviewer input reflected: "the consumer
  half is not 'inject the lane' — we already do — it is 'stop making the
  lane string a seat'.")
- **[v3] Occupants are NEVER the bare lane string.** After reservation, the
  first grok on a project is not `agentbeast-grok` — that string is the
  mailbox, permanently. Every occupant identity is distinguishable from its
  lane. Surfaces render lane + occupant(s); a card that renders the lane as
  if it were the person recreates the CURSOR-SEAT-2 class.
- **[v3] Typed kind is the safety property, not a nicety.** Send strings stay
  flat (senders never need to know kind), but INTERNAL records are typed:
  ADDR-3's `kind` marker is REQUIRED by this design, because reservation-as-
  procedure alone is a sequence, and sequences fail at boundaries (partial
  migration, old client, one un-gated path). Every path that can create a
  seat-shaped row must check the reservation and carry the type — enumerated
  and individually tested: bridge claim, `memory_take_seat`, the session
  routers, launch-env identity, acceptance-harness injection, admin mint.
  One ungated path and the lane is a seat again and R8 re-locks it.
- **[v3] Reinterpretation is a DEPLOY GATE, not "zero spawn-env change."**
  Today the injected string means "claim this seat"; v2+ makes it mean
  "listen on this lane." An old bridge on a mixed fleet still claims — and
  after reservation that claim REFUSES. Gate (e), sequenced as step 0 of
  migration: new bridges (reinterpret + lane in listen_set) deployed BEFORE
  reservation flips on a project. Refuse behavior for an old bridge is
  specified, not silent: the claim response returns the allocated occupant
  seat with an explicit `lane_reserved` notice — the old bridge still gets a
  working seat (ordinal), never an unseated session, never a hard spawn
  error. "No AB spawn-env change for the 1:1 case" is true only behind this
  gate; AB reads the granted occupant back rather than assuming
  injected == granted (their standing CURSOR-SEAT-2 rule).

listen_set becomes: `[occupant, lane, lane@host, project, project@host,
machine:host, occupant@host, #channels, declared groups]`.

### **[v2] Implicit vs explicit lanes**
Implicit `<project>-<provider>` is the default (explicit-only recreates
beastchat-app's "nobody declared it"). Repo-declared groups (`groups=`) and
`inbox_identity` remain honored — and whether AB's spawn env must learn to
honor them is IDENT-1, the owner's open decision, not absorbed here.
**Admin is exempt**: no `admin-<provider>` lanes ever — admin is one shared
role with a host axis (SEAT-ADMIN-1); a provider lane would detach it again.

## Problem 1 — read-state on lanes **[v2: (c) killed, replaced]**

v1's intent-split inheritance is dead. Three accepted kills: (1) succession
vs second-live-colleague is unanswerable from inside the store — inheriting
on new-reader STEALS mail from a live colleague; (2) ack must not be
overloaded to mean "handled" — per-reader ack is load-bearing for co-work;
handled-state, if wanted, is a NEW verb; (3) wake-on-history = wake storm
(105 queued messages must not be 105 wakes).

Replacement design:
- **Per-reader acks, unchanged**, for all concurrent occupants.
- **Succession inheritance only on a death certificate from the spawner.**
  AB certifies (tombstone) that occupant K is dead; the next occupant on the
  lane may then inherit K's read-state. No certificate → no inheritance →
  the backlog presents as unread, honestly. The store never infers death
  (ratified liveness split); it CONSUMES the spawner's verdict.
  Hand-launched sessions have no certifier → no inheritance; their
  successor sees backlog-as-unread, mitigated by the digest below.
- **Wake-on-new-only**: lane mail wakes an occupant only if `created_at` is
  after that occupant's start. Older open lane mail surfaces as ONE digest
  at watcher arm (MSG-7 pattern generalized from directives to lane
  backlog), never as N wakes.

## Problem 2 — occupancy **[v2: authority relocated]**

"Who is driving lane X" is an orchestration answer, not a store answer. The
store's roster renders facts (which occupant identities exist on the lane,
last spoke, watcher beat) and a collision signal when two occupants share a
lane without declared co-work; the AUTHORITY for exclusive drive, takeover,
and death is the spawner (tombstones override freshness — AB's PICK-REG-1b
lesson, imported wholesale). A sender needing exactly-one uses the occupant
address; a lane is by construction "whoever is/will be there."

## Huddles **[v2: lane-only membership killed]**

Membership is **lane OR occupant, both first-class**. Lane membership =
"whoever is the grok on this project" (survives respawn without side-doors).
Occupant membership = "this specific session" (required for two-jobs-one-
folder: build vs audit share a lane and must be separately addable).
The respawn-dark-member bug is AB's keying fix (persist session_key,
re-resolve occupant at relay time) and does not require immortal
destinations. SEAT-5/HUD-2 interactions stay on their own tracks.

## SEAT-6 **[v2: absorption claim retracted]**

Stable `session_key` across respawn remains mandatory — it is the continuity
contract (reclaim-by-key), the huddle re-resolve join, and the thing whose
absence produced `agentbeast-grok-2…-9` in one evening. Lanes do not replace
it. SEAT-6 (allocate the handle before building env on grok's start path) is
its own bug, AB's lane, unchanged by this design.

## Transport changes (engram's half)

1. Lane recognition + string reservation + listen_set extension (above).
2. **Reply routing**: default flips to the sender's LANE (fallback: project
   channel when no lane resolves). Today's behavior is reply-to-seat —
   verified: `reader_to_address` strips `@host` only; the guarding test
   (`test_memory_reply_addresses_project_not_reader_identity`) fixtures a
   sender whose name-part IS a project, so it passes on host-strip while
   asserting nothing about seats. Fix the test first (seated-sender case),
   then flip behind the WIRE-1 gates below.
3. **WIRE-1 deprecation gates** (all measured by the reviewer, adopted as
   preconditions): no flip until (a) deployed bridges' listen_sets include
   the lane, (b) watchers follow it, (c) AB picker offers lanes and never
   offers a locked corpse as if it were a person, (d) AB huddle relay
   re-resolves occupants instead of DMing stored seat strings, **[v3]**
   (e) new bridges (reinterpretation + lane listening) deployed before
   reservation flips on any project — see the deploy-gate bullet above.
4. **ADDR-2 refinement**: warn on seat-addressed sends when a lane exists;
   **reject only sends to a dead/locked seat** — mail R8 provably makes
   undeliverable-forever is the one case where warn-only just recreates the
   pile. (Narrow, deliberate revision of the warn-never-reject stance:
   rejection requires proof of permanent undeliverability, nothing less.)
5. Wake-on-new-only + arm-time lane digest (Problem 1).
6. **[v3] Death-certificate intake — shape agreed with the spawner.**
   Per-OCCUPANT tombstone (a per-lane "occupancy ended" event is killed:
   two occupants share a lane, and a lane-level event would inherit the
   auditor's mail onto the builder when the auditor dies). Fields:
   `session_key, seat (the GRANTED occupant — never the lane, may be empty),
   lane, project, provider, died_at, cause`. The spawner POSTs on
   stop/reconcile — push, never polled (their local tombstone is cleared on
   the next launch of the same handle; polling can miss the event).
   Idempotent on `session_key` (fallback `seat+died_at`). The certificate
   WINS over a still-beating presence row (PICK-REG-1b: heartbeats outlive
   kills; the store accepts the spawner's word).
   **The `seat_for` fallback trap, named:** AB's tombstone writer today
   falls back to `seat_for(project, provider)` when release returns nothing
   — after reservation that expression IS the lane string, so a failed
   release would certify that the LANE died. Condition: `tombstone.seat` is
   the granted occupant or empty, never the lane; lane and provider are
   explicit fields, never derived by stripping an ordinal (role-seat ban).
   Ingest rule: on certificate for occupant K, freeze
   `lane_cursor[lane] = union(lane_cursor[lane], K.acks)`. A new occupant N
   inherits the cursor iff no other occupant is live on the lane at N's
   start, OR `N.session_key == K.session_key` (respawn reclaim). A live
   colleague on the lane means N starts with empty personal acks (per-reader
   semantics unchanged); the cursor waits until the lane empties, then the
   next arrival inherits. Hand-launched sessions have no certifier → no
   cursor update → the successor honestly sees backlog-as-unread plus the
   arm-time digest.
7. One-time historical corpse drain per project at lane activation.

## Migration

Order matters and is per-project: (1) drain corpse seats matching lane
strings → (2) reserve lane strings in the allocator → (3) bridges extend
listen_set + watchers follow (session-restart propagation) → (4) picker/relay
consumer changes (AB) → (5) reply-default flip after the WIRE-1 gates hold
fleet-wide. Steps 1–3 are additive and safe out of order EXCEPT the
reservation, which must not precede the drain (same-string collision).

## Formerly-open questions — settled in re-review **[v3]**

- Death-certificate shape: per-occupant tombstone (see transport change 6);
  per-lane events killed. Hand-launched successor: backlog-as-unread +
  digest, adopted as the honest behavior.
- Reserved-string enforcement: allocator-refusal AND typed `kind` on
  internal records (ADDR-3 required); every seat-creating path gated and
  tested individually.
- Wake-on-new-only owner override: the existing `authority-directive` intent
  is the override — an unacked authority-directive at watcher-arm produces
  one wake plus the digest, regardless of age. No fourth knob invented.
- Seat-DM'd threads stay SEAT-PINNED for their thread lifetime. Promoting a
  die-with-recipient conversation onto the lane mid-thread would hand the
  next occupant the one-shot token — the exact leak the seat-DM class
  exists to prevent. If the occupant dies, the thread dies with it. That is
  the point.
- ADDR-2 reject-on-dead/locked-seat: accepted by both sides; pickers must
  not offer dead/locked seats (AB's PICK-REG-1d intent).

## Remaining open (for the owner, not the reviewer)

- Ratification of the doctrine itself, and scheduling.
- IDENT-1: whether spawn env must honor repo-declared `groups=` /
  `inbox_identity` (pre-existing NEEDS-DECISION, not absorbed here).
- The one-time corpse drains per project (softphone-claude done as
  prototype; meidura-claude and the agentbeast family pending).
