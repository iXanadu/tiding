# Comms Build Plan — roles, sequence, and gates (v2)

**2026-08-18. Drafted by engram-claude (incarnation -7) at the owner's
direction; the owner set the ordering criteria and named this document the
durable plan of record.** Source of truth for WHAT: [comms-outcomes.md](comms-outcomes.md)
(six locked outcomes, ADDR-REVIEW-1). This document adds ORDER, ROLES, and
GATES. Sequence, never schedule — no clock estimates, by standing rule.

## Status — swept 2026-08-18 ~03:50Z (first execution night)

**CLOSED, all audit-passed / deployed / live-verified:** Steps 1, 2, 3, 4,
6, 7, 8, 9, 12, 13, 14, 15 — twelve of twenty in one owner-driven night,
plus the REG-DEATH-1 prerequisite (with a ratified Lock-1 amendment:
evidence-of-life-after-died_at voids a cert). Step 15's closer was the
owner's genuine tap (03:45:13Z, human path verified by source), after two
false starts the loop itself caught: an unpainted surface (the owner's
screenshot was the audit) and a headed-owner-session line-crossing
(disclosed, ruled, remediated; HEADED-OWNER-1 now a written rule in both
ledgers, DEC-XSS-1 found and fixed before the owner ever touched the page).

**OPEN:** Step 5 (wrapup mail-drain — two-stage; closes when the PM
session's own wrapup drains its estate) · Step 16 (status digest —
dispatched to agentbeast-claude under the Band-E provision; shape to the
auditor before build) · Steps 10–11 (Band D — reader-gated: one stale
webone bridge + the owner's two Desktop bridge processes; re-census before
any sub-step) · Steps 17–20 (Band F — the owner's split-timing and
#channels decisions, plus AB/app surface work). DEC-NAV-1's Chat-inline
half stays PARTIAL post-15. Ops: cron wiring for /admin/inbox/climb +
/sweep (admin credential + cadence) is announced-before-wired.

**Night's ledger adds:** REG-DEATH-1 (fixed), DEPLOY-3, HEADED-OWNER-1
(engram side; AB holds the twin). Eight engram prod bounces + three hub
bounces, every one announced in-thread first, zero incidents.

## Ordering doctrine (the owner's criteria, verbatim intent)

1. **Error-free first.** Nothing may break the running fleet. Concretely:
   - Uniform substrate before any behavior change (every deployed reader on
     the same bridge before the wire moves — the WIRE-1 lesson).
   - Additive before subtractive. New capability lands and soaks beside the
     old path; the old path stops only after the new one is proven on every
     deployed consumer.
   - One behavior flip per deploy window, each with a pre-registered
     verification and a stated rollback, announced in-thread before executing
     (same rule as prod bounces).
2. **Functioning as intended second.** Each item closes against the outcome
   it serves, verified live — a claim is not a close. The standing acceptance
   harness (`scripts/accept.sh`) and rerunnable live probes are the
   instruments.
3. Speed is not a criterion. Anything that trades safety for pace is ordered
   wrong.

## Roster — names and roles

Roles are attached to **lanes** (durable, per O4), not incarnation ordinals —
whoever is, or is next, that provider on that project holds the role.

| Name | Role |
|---|---|
| **Rob** (owner) | Names each item to start it; rules on every Open Decision; runs acceptance taps himself; convenes huddles. Owner's explicit word is the only GO. |
| **engram-claude** | **Driver** and transport owner. Keeps this document current, sequences the work, builds every engram-column item, announces flips, maintains the verification record in project memory. |
| **agentbeast-claude** | Orchestration builder. Builds the AB column: relay room-only, launcher read-back, decision-object surface, status digest, transcript retention/browse, credential separation. Owns AB's tracker mapping of these items. |
| **agentbeast-grok** | **Adversarial auditor.** Reviews each phase's design before its flip, holds the pre-registered verification criteria, and vetoes a flip whose evidence is short. (Holds the HUDDLE-SPEAK-1 pin already.) Supervises huddles when convened, per the pattern that worked. |
| **agentbeast-app** | App-surface builder. Renders what AB's column defines (decision objects, digest, lane-keyed DM view) on the app; executes its side of the project split when the owner names it. |

Cross-lane asks travel as mail to the project channel (O2); huddles are for
concurrent design work, convened by the owner.

## Inputs beyond the outcomes doc

**The 2026-08-18 seat-succession test** (run live: clean close of -5, restart,
owner DM):
- **T1** Lane succession works — the successor inherited the predecessor's
  exact seat via launcher seat-file continuity. O4's reusable-names half
  functions today.
- **T2** The collision doctrine fights succession — the predecessor's bridge
  still heartbeat ~20 min after its clean goodbye (agent-death ≠
  session-death); the detector read succession as a collision and its banner
  ordered lane flight; obeying it burned the lane. Banner + startup skill are
  pre-outcomes doctrine. → Step 8.
- **T3** Discovery survived — the owner's DM found the moved seat; delivery +
  wake closed end-to-end in under a minute.
- **T4** RELAY-1 reproduced on the test itself — a DM sent via AB's surface
  arrived stamped VERIFIED OWNER. Benign this time; the class is escalation.
  → strengthens Step 3; the envelope field itself stays parked until the
  owner names it.
- **T5** A goodbye does not stop the heartbeat — climb's "death evidence"
  cannot be the farewell alone. → Step 12 design input.

**Standing constraints:** WIRE-1 (a wire contract has as many consumers as
there are deployed readers); BRIDGE-ROLLOUT-1 (spoke bridges are stale — a
bridge feature isn't real until the sweep runs); MAIL-1's warning (watchers
today wake on huddle rows through the default inbox view — removing those
rows before the wake primitive is everywhere would deafen the fleet; this is
the plan's most dangerous edge and Step 10's ordering exists because of it).

---

## The sequence

Numbered steps, strictly ordered between gates; steps inside the same band
may run concurrently where marked ∥. Every step starts on the owner's name.

### Band A — Substrate (make the fleet uniform and observable; zero behavior change)

1. **Bridge rollout sweep** — engram-claude. Update cc-memory installs on
   every spoke; record the swept bridge version per box in project memory.
   *Verify:* every box's presence beats carry host (PRES-2) and watchers die
   loud (WATCH-2). **Hard gate for every WIRE change below — no wire change
   lands on mixed readers. Provider-internal items (e.g. Step 3, AB's /speak
   auth) are not gated by the sweep; ∥-marked Band A items run concurrently.**
   *(Scope confirmed by PM at kickoff, on agentbeast-claude's question.)*
2. ∥ **Small fixes** (engram §9) — engram-claude. ADDR-REG label noise,
   `preferred_seat` refresh-overwrite. *Verify:* register output on
   admin/owner rows; refresh probe.
3. ∥ **Credential separation** (AB §7, HUDDLE-SPEAK-1) — agentbeast-claude.
   Agents stop holding the owner bearer; `/api/huddle/speak` refuses or marks
   non-owner callers. Security-urgent independently; T4 is tonight's
   evidence. *Verify:* a peer attempt to speak as owner is refused/marked,
   probed live.
4. ∥ **Transcript retention confirm + browse** (AB §6) — agentbeast-claude
   with agentbeast-app for the surface. Confirmation ("stopped deleting")
   before surface. *Verify:* a named old transcript is retrievable.

### Band B — Drain discipline (additive; shrinks mail debt before lifecycle work)

5. ∥ **Wrapup mail-drain step** (engram §7) — engram-claude. Skill step +
   one capability-neutral AGENTS.md line. *Verify:* next clean close drains
   its estate to zero unread (the -5 close is the template).
6. ∥ **Estate survey at startup** (engram §8) — engram-claude. Extend the
   watcher's pre-arm digest to project-subtree, grouped by node, live/dead
   owner split. *Verify:* startup digest on a project with known open mail
   matches a hand count.
7. ∥ **Reply-to-channel default** (engram §5) — engram-claude. Cross-project
   replies target the requesting project's channel per O2. *Verify:* live
   cross-project round-trip lands on the channel, not the asking seat.

### Band C — Addresses (additive registration first, then the allocator)

8. **Project registration at the root** (engram §6) — engram-claude. The tree
   gets a verifiable root; typo detection; dormant-project listing. Purely
   additive. *Verify:* register lists every known project; a typo'd send
   draws the advisory.
   **Includes T2's fix:** succession-aware collision handling — the detector
   distinguishes lane succession (predecessor bridge lingering post-goodbye)
   from true collision; the banner stops prescribing lane flight; the startup
   skill's seat steps are rewritten to the O4 model. *Verify:* rerun the
   2026-08-18 test — successor keeps its lane, no banner, no ordinal minted.
9. **Allocator** (engram §2) — engram-claude, audited by agentbeast-grok
   before deploy. Reserve bare `{proj}` and lane strings; keep lowest-gap;
   unify the skip ladder into one shared helper (ADDR-REG-1); provenance on
   session_key. **Mail-gating (R8 park) stays in place in this step** — its
   removal is Step 11, after wake-not-letter lands. ∥ **Launcher read-back**
   (AB §2) — agentbeast-claude: never declare ordinals, read the grant from
   `/session/seats`. *Verify:* 12-way race probe still allocates clean;
   reserved strings refused; a launcher spawn reads back its granted seat.

### Band D — Wake-not-letter (the core joint flip; one step at a time)

10. **The flip, in four sub-steps, each announced in-thread first**
    (engram §1 + AB §1) — engram-claude + agentbeast-claude, verification
    criteria held by agentbeast-grok:
    a. engram ships the wake primitive; bridge/watcher wake on it; deployed
       fleet-wide (Band A makes this real).
    b. **Soak with both paths live** — huddle still writes rows AND wake
       fires. *Gate:* every deployed watcher demonstrably wakes on the
       primitive in a live huddle.
    c. AB relay flips to room-only posting — no inbox fan-out.
    d. engram stops accepting huddle fan-out as inbox rows.
    *Rollback at every sub-step:* the previous path is still deployed; revert
    is a config flip, not a rebuild. *Verify (final):* a live huddle where
    every participant wakes and the room's inbox row-count is zero.
    **Consumer acceptance asks (agentbeast-claude, accepted at kickoff, part
    of the Band D contract):** (a) a probe path verifying "watcher X woke on
    event Y" without live-firing at the owner; (b) both-paths delivery counts
    readable during the 10b soak, so parity is measured rather than claimed;
    (c) AB's 10c flip sits behind a config flag for symmetric rollback.
    **Auditor criteria (agentbeast-grok, drafted at kickoff, pre-registered
    for Band D):** server-side ordering as the only wake evidence (agent
    self-report is not evidence); 10a not closed until every deployed watcher
    on every swept box wakes on the primitive; a soak huddle that wakes only
    via lingering inbox rows proves nothing; mid-job interrupt wake is a
    separate claim needing its own named probe; rollback needing a redeploy
    means the step is ordered wrong.
11. **Remove mail-gating of seat assignment + grace-window machinery**
    (rip list) — engram-claude. Only now safe: O6 killed the echo that parked
    seats, O5 makes deep mail ephemeral. *Verify:* seat churn probe — claim,
    mail, release, re-claim — allocates lowest-gap with no parking.

### Band E — Lifecycle (the design-hard half; design reviewed before build)

12. **Handled-vs-read discriminator** — engram-claude designs,
    agentbeast-grok adversarial review BEFORE build (this is the open hard
    problem: chatter dies by being read; an ask dies only answered or
    declined; T5 rules out farewell-as-death-evidence). No code until the
    design survives review.
13. **Climb** (engram §3) — engram-claude. Unhandled asks rise to the nearest
    living ancestor on death evidence (incarnations) / dormancy-while-
    ancestor-active (lanes). *Verify:* a planted unhandled ask on a killed
    incarnation surfaces at its lane; a handled one does not.
14. ∥ **Sweep tuning** (engram §4) — engram-claude. Epoch expiry for deep
    mail per O5. *Verify:* planted deep chatter expires; planted asks
    survive to climb.
15. ∥ **Decision-object surface** (AB §3) — agentbeast-claude +
    agentbeast-app. Asks-to-owner become stateful objects
    (open/answered/declined/expired), replacing letters-to-Rob. *Verify:* a
    live ask round-trips owner-answer → asking session notified.
    *(Pulled forward 2026-08-18 by PM re-sequence under this band's own
    "may start earlier" provision — no mail-wire dependency; auditor
    reviews the API shape before build. Band D's gate is untouched.)*
16. ∥ **Status digest** (AB §4) — agentbeast-claude + agentbeast-app. Agent
    progress reports leave the DM plane. *Verify:* one morning's digest
    replaces the day's status letters, counted.

### Band F — Structure and rips (each an owner decision made executable)

17. **agentbeast-app project split** (O2 corollary) — owner names timing;
    agentbeast-claude + agentbeast-app execute; engram-claude advises on
    memory-row disposition. cfg + launcher injection + row disposition move
    together, one arc.
18. **`#channels` rip** — engram-claude, after a deployed-consumer check
    (WIRE-1 discipline; grep the shipped bridge + AB's tree, then a
    deprecation soak).
19. **Remaining rip list** — engram-claude: `groups=`/`inbox_identity` cfg
    (dies with 17), `-app` key-suffix convention (dies with 17),
    elevation-ladder design docs marked superseded.
20. **Lane-keyed DM chat view** (AB §5) — agentbeast-claude + agentbeast-app.
    Interim habit (DM the lane) already works; this re-keys the existing
    session DM view into one continuous owner↔lane conversation.

## Stays parked (not scheduled here)

RELAY-1 envelope field (T4 strengthened the case; owner reopens by name) ·
MAIL-1 surface disposition (Band D changes its facts; re-ask the owner
after) · physical deletion of archived rows (never required) · task tracking
proper (orchestration layer per the July division-of-labor ruling; where it
lives stays open).

## Kickoff huddle agenda (when the owner convenes)

1. Ratify this document; each named lane confirms its column.
2. agentbeast-claude: map the AB column onto their tracker; flag conflicts.
3. Agree the wake-primitive interface (the Band D contract) — engram proposes,
   AB consumes, grok holds the criteria.
4. Owner names the first item(s). Band A Step 1 is the driver's
   recommendation for first-named.

## Coordination protocol

- This document is the shared sequence of record; changes are commits, argued
  in the open. Lanes may re-order ∥-marked steps within a band; band GATES
  are the contract.
- Every flip announced in-thread before executing. Every step closes with its
  named live verification, recorded in project memory.
- Verification results and step completions journal to engram project memory
  (`fix/<step>` keys); the owner reads state from this doc + the ledger, not
  from chat scrollback.
