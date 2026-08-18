# Comms Build Plan — sequencing the ADDR-REVIEW-1 build orders

**2026-08-18, engram-claude-7, drafted at the owner's direction.**
Source of truth for WHAT: [comms-outcomes.md](comms-outcomes.md) (the six locked
outcomes). This document adds only ORDER, DEPENDENCIES, OWNERSHIP, and GATES.
Sequence, never schedule — no clock estimates anywhere, by standing rule.

Participants: **engram** (this repo), **AgentBeast** (their tracker), and
**agentbeast-app** (becomes its own project per O2; split timing is an owner
decision placed in Phase 4). Each item still starts when the owner names it —
this plan is the order he names them in, not blanket authorization.

---

## New inputs since the outcomes doc (the 2026-08-18 seat-succession test)

Run live tonight: predecessor `-5` closed cleanly; successor restarted in the
same lane; owner DM'd the successor. Findings, each placed in a phase below:

- **T1 — Lane succession works today.** The successor inherited the
  predecessor's exact seat via launcher seat-file continuity. O4's
  reusable-names half is functioning.
- **T2 — The collision doctrine fights succession.** The predecessor's bridge
  was still heartbeating ~20 min after its clean goodbye (agent-death ≠
  session-death), so the detector read succession as a 2-nonce collision and
  its banner ordered the successor to a new seat. Obeying it burned the lane
  (`-5` orphaned, `-7` minted). The banner and the startup skill are
  pre-outcomes doctrine and must become succession-aware. → Phase 2.
- **T3 — Discovery survived the burn.** The owner's DM found the live seat via
  the register. Delivery + wake closed end-to-end in under a minute.
- **T4 — RELAY-1 reproduced on the test itself.** The DM was described as
  "from AgentBeast" and arrived stamped `✓ VERIFIED OWNER` — the relay path
  wears the owner's stamp regardless of author. Benign this time; the incident
  class is escalation. → Phase 0 (AB credential separation) + the quarantined
  RELAY-1 envelope field, owner reopens by name.
- **T5 — A goodbye does not stop the heartbeat.** "Death evidence" for climb
  (engram 3) cannot be the farewell alone; the lingering bridge beat must be
  accounted for or the collision window will misfire forever. → Phase 3
  design input.

## Standing constraints that shape the order

- **WIRE-1**: a wire contract has as many consumers as there are DEPLOYED
  readers. Every phase that changes what the inbox serves or how wake fires
  needs a deployed-reader check first, not a maintainer's yes.
- **BRIDGE-ROLLOUT-1**: spoke boxes run stale bridges. Any bridge-carried
  behavior (wake primitive, banner changes) does not exist fleet-wide until
  the rollout sweep runs. This makes the sweep a **prerequisite**, not
  hygiene.
- **MAIL-1's warning**: agent watchers currently wake on huddle rows through
  the default inbox view. Wake-not-letter removes those rows — so the wake
  primitive must be live in every deployed watcher BEFORE the rows stop, or
  the flip deafens the fleet. This is the plan's most dangerous edge.

---

## Phase 0 — Ground-clearing (independent, cheap, de-risks everything after)

No cross-dependencies; can run in any order, several in one arc.

| Item | Owner | Notes |
|---|---|---|
| Bridge rollout sweep (BRIDGE-ROLLOUT-1) | engram | Update cc-memory installs on every spoke; establish the "feature shipped → rollout swept" habit. **Prerequisite for Phase 1's flip.** |
| Small fixes (engram §9) | engram | ADDR-REG exempt/person labels; `preferred_seat` refresh-overwrite bug. |
| Reply-to-channel default (engram §5) | engram | Small, aligns behavior with O2 immediately. |
| Wrapup mail-drain step (engram §7) | engram | Skill + one capability-neutral AGENTS.md line. Reduces mail debt now, shrinking what climb/sweep must later carry. |
| Estate survey at startup (engram §8) | engram | Watcher digest exists (fired tonight); extend to project-subtree, grouped by node, live/dead owner split. |
| Credential separation (AB §7) | AB | HUDDLE-SPEAK-1. Security-urgent independently; T4 reproduced the stamp problem tonight. |
| Transcript retention + browse (AB §6) | AB | Confirmation first ("stopped deleting"), surface second. |

**Gate to Phase 1:** rollout sweep verified (every spoke bridge current), and
the wake-primitive design agreed between engram and AB (their relay is the
other half).

## Phase 1 — Wake-not-letter (the core joint flip; engram §1 + AB §1)

The single biggest lever: kills the 90.2% traffic class at the source.

1. engram ships the wake primitive for room participants; bridge/watcher wake
   on it. Deploy fleet-wide (Phase 0's sweep makes this real).
2. **Soak with both paths live** — huddle still writes rows, wake also fires;
   verify every deployed watcher wakes on the primitive.
3. AB flips the relay: posts to the room only, no inbox fan-out.
4. engram stops accepting huddle fan-out as inbox rows.

Ordered so that at no point does a deployed watcher depend on rows that have
stopped arriving (the MAIL-1 edge). Verification: a live huddle where every
participant demonstrably wakes, and inbox row-count for the room is zero.

**Unlocks:** removal of R8 mail-gating machinery (Phase 2), the rip of grace
windows, and MAIL-1's eventual disposition (the owner's inbox becomes true
letters only).

## Phase 2 — Addresses (engram §6 + §2 + AB §2 + T2)

1. **Project registration at the root** (engram §6) — the tree gets a
   verifiable root; typo detection; dormant-project listing. Do this FIRST:
   reserving bare `{proj}` strings (next step) needs an authoritative project
   list.
2. **Allocator** (engram §2): reserve bare `{proj}` and lane strings; keep
   lowest-gap; unify the skip ladder into the shared helper (ADDR-REG-1);
   provenance rides session_key; remove mail-gating of assignment (gated on
   Phase 1 landing).
3. **Succession-aware collision handling** (T2, new): the detector must
   distinguish "successor in its lane while the predecessor's bridge lingers"
   from a true collision; the banner must stop prescribing lane flight; the
   startup skill's seat steps get rewritten to the O4 model (listen on the
   ancestor chain; never flee your lane on a banner's word).
4. **Launcher read-back** (AB §2): never declare ordinals; read the grant from
   `/session/seats`.

Verification: repeat tonight's test — clean close, restart, and the successor
keeps its lane with no banner and no ordinal minted.

## Phase 3 — Lifecycle (engram §3 + §4; AB §3 + §4)

1. **Handled-vs-read discriminator** (design first — it gates climb and is the
   open hard problem: chatter dies by being read; an ask dies only answered or
   declined). T5 input: death evidence must account for the lingering bridge
   beat.
2. **Climb** (engram §3): unhandled asks rise to the nearest living ancestor.
3. **Sweep tuning** (engram §4): epoch expiry for deep mail per O5.
4. **Decision-object surface** (AB §3) and **status digest** (AB §4): drain
   the asks-to-owner and status-spray classes into their proper surfaces, so
   climb carries only what genuinely remains.

AB §3/§4 have no hard dependency on climb and may start earlier if the owner
names them — placed here because together they define what is LEFT for the
mail plane to handle.

## Phase 4 — Structure and rips (owner decisions become actions)

| Item | Owner | Notes |
|---|---|---|
| agentbeast-app project split (O2 corollary) | owner names timing; AB+app execute; engram advises on memory-row disposition | cfg + launcher injection + row disposition move together. |
| `#channels` rip | engram, after consumer check | O2 obsoletes; confirm no live consumer first (WIRE-1 discipline). |
| Rip list execution | engram | `groups=`/`inbox_identity` cfg, R8 park + grace machinery (post-Phase 1), `-app` suffix (dies with the split). |
| Lane-keyed DM chat view (AB §5) | AB | Interim habit (DM the lane) already works; the view re-keys the existing session DM surface. |

## Stays parked (not scheduled by this plan)

RELAY-1 envelope field (quarantined; T4 strengthens the case — owner reopens
by name) · MAIL-1 surface disposition (Phase 1 changes its facts; re-ask the
owner after) · physical deletion of archived rows (never required) · task
tracking proper (orchestration layer per the July division-of-labor ruling;
where it lives stays open).

---

## Coordination protocol

- Engram and AB each work their own column; **joint flips (Phase 1 steps 2–4)
  are announced in-thread before executing**, same rule as prod bounces.
- Every phase ends with a live verification named in advance, not a claim.
- This document is the shared sequence of record; changes to it are commits,
  argued in the open. AB's half is theirs to re-order within a phase; the
  phase GATES are the contract.
