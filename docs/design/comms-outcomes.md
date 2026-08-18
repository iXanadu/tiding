# Comms Outcomes — the Addressing & Messaging Review (ADDR-REVIEW-1)

**2026-08-17, owner + engram-claude-5, ~5-hour working session.**
Status of each outcome is marked. Everything here was derived from measurements
taken live during the session; the evidence base is summarized inline. Project
memory holds the working record (`decision/comms-outcomes-2026-08-17`,
`fix/o6-echo-archive-2026-08-17`, `incident/owner-token-huddle-speak-2026-08-17`).

Method, fixed by the owner at the outset: **define outcomes first, then rip out
everything that does not march toward an outcome.** Outcomes are targets to
build toward, not implementation decisions.

---

## The outcomes

### O1 — A project is a channel *(LOCKED)*

A project name, declared in `.engram.cfg`, **is** a durable comms channel. Mail
sent to it is accepted and held at any time, whether or not any participant is
alive or has ever existed. Every session working the project — any provider,
any seat — listens on it automatically. `{proj}@machine` is a per-box
sub-channel, optional for everything except maintenance roles.

*Mostly shipped already; this ratifies the strongest part of the existing
system.*

### O2 — Projects are peers *(LOCKED)*

Inter-project dependency is expressed as **requests mailed to the other
project's channel** (the VASTATE→MediaStudio pattern) — never as shared
identity, shared memory buckets, or family structures. Each project owns its
messages and its memory. Cross-project replies target the **requesting
project's channel**, never the asking seat — the asking session may be dead by
the time the answer comes.

*Corollary, agreed: `agentbeast-app` becomes its own project. Its cfg drops
from three mechanisms (+25 lines of apologizing comments) to one line.
`groups=` and `inbox_identity` rip — both were patches over the mis-drawn
boundary.*

### O3 — Messaging owes AB nothing *(LOCKED)*

Agent↔agent messaging runs entirely on engram. Any agent, any provider,
spawned bare from a CLI: identity obtained **eagerly at spawn** (BEAT-1 already
proves the mechanism — first beat fires at bridge startup), watcher armed
**by the bridge** (mechanics funnel), able to message any channel. The human
leg is deliberately AB's franchise: engram spools, AB renders.

*Two funnels rule: anything that must be TRUE for every agent goes in the
bridge (identical code, all providers); anything that must be JUDGED goes in
`AGENTS.md` (verified symlinked to all four providers). Named gap: hand-launched
Cursor has no bridge at all (CURSOR-IDENT-1).*

### O4 — Addresses are a tree *(LOCKED as amended)*

```
{proj}                    channel      durable    the root; every address parses back to it
{proj}@machine            sub-channel  durable    box-pinned
{proj}-provider           lane         durable    "whoever is / is next the <provider> on <proj>"
{proj}-provider-N         incarnation  MORTAL     one session
```

Every string denotes exactly one entity of one kind — **position in the
grammar is the kind** (this is the ADDR-3 fix without building ADDR-3). Bare
`{proj}` and bare lanes are **never grantable as seats** (two live squat
defects found by experiment during the session: a cursor corpse holding
`seat/engram`; the bare-lane cooksbayouboy squat). A session **listens on its
ancestor chain**. Mail **delivers downward** (to sessions under the addressed
node) and **belongs upward** (to every ancestor).

**The amendment (owner's rule, adopted after adversarial review):** names are
**addresses, reusable, filled lowest-gap-first** — because under O5/O6 nothing
ever parks a name. **Identity is the session key** (already exists, never
reused, invisible) — what death certs, provenance and records pin to. This is
the three-axes doctrine (address ⊥ identity) finally applied to ordinals.
Launchers never declare ordinals — declare nothing (or a meaningful name) and
**read back the grant**.

*Why the climbing ordinals happened (measured): releases mostly ran; the names
were guarded by unresolved mail (R8) and grace windows. Every fresh-context
spawn is a new key by design, so lowest-free degenerated to append-forever.
The walk is cured by decoupling, not by allocation policy.*

### O5 — Depth is fragility; durability lives at the root *(LOCKED)*

The **project channel and memory are the only durable stores.** Every address
qualifier is a condition on future readership ({proj}: someone works this
project again · +provider: on this provider · +N: this exact process lives).
Below `{proj}` is **ephemeral by declaration** — lane and incarnation mail
serves live coordination and dies with its epoch.

**One exception: unHANDLED asks climb to the nearest living ancestor.**
Handled ≠ read — the current sweep cannot tell "read and done" from "read and
abandoned" and must learn the difference. Chatter may die by being read; an ask
may only die by being answered or explicitly declined.

Send shallow by default. Replies default to channels. A session's graceful end
includes draining its own estate (the wrapup mail-drain step). Coverage layers:
**wrapup** (graceful ends) → **72h read+stale sweep** (forgotten) → **climb**
(unhandled asks) — each backstopping the one above.

*Measured basis: mail value ∝ sender/reader concurrency. Killing all stale
deep mail loses ~nothing (the fleet's real state system is memory-first and
already won that job); live coordination is mail's only high-value use.*

### O6 — Conversations are not letters *(LOCKED — "the biggest find")*

Owner's formulation: **"Huddle traffic is not message traffic. We are treating
a zoom meeting like an e-mail inbox."**

A huddle post is an utterance in a room: it **wakes** present participants
(ephemeral ping), is recorded **once** in the room transcript, and is **never
stored as per-participant durable mail**. Room closes → nothing to drain.
Catch-up = read the transcript, not N letters.

Final form, from the admin@webone evidence: the distinction is
**conversation-vs-letter, not huddle-vs-DM.** A rapid exchange between
*concurrent* sessions is a meeting whichever mechanism carries it (relay,
fan-out thread, bare DM pair — agents meet over the letter plane because it is
the only plane two agents share). A **letter** is what you send when the other
side may be absent — the only thing that ever deserved durable per-recipient
mail.

*Measured basis: 17,999 of 19,953 inbox rows ever (90.2%) were meeting echo —
~3 stored copies per utterance (room record + one per non-author participant).
Zero parked seats were parked by echo; the graves were covered in true
letters. Fleet-wide true correspondence over all history: under 2,000 rows.
Retroactively explains MAIL-1 (the owner made this exact finding in July and
lacked the category to name it), and the "lazy resolve" culture (agents were
correctly refusing to file closings on speech).*

---

## The traffic classes (all measured in the owner's own 98-letter pile)

| Class | What it is | Where it belongs |
|---|---|---|
| **Meeting echo** | fan-out copies of concurrent conversation | room transcript + wake; never the inbox |
| **Status spray** | agents' work journals delivered as letters (~55 of the owner's 98) | session journals / wrapup summaries / a digest |
| **Asks to the owner** | decisions, credentials, go/no-go (~11 of 98; ~3 still live) | **decision objects on a stateful AB surface** — open/answered/declined/expired — never inbox rows ("any message to me is dead") |
| **True letters** | correspondence where the other side may be absent | the inbox — finally small enough to mean something |

## The three planes

| Plane | Transport | Record | Who |
|---|---|---|---|
| **Chat** | AB→harness stdin (claude: CLI/remote only) | session transcript | human ↔ one session, live |
| **DM** | engram `memory_send` | inbox table, permanent | anyone ↔ any address |
| **Meeting** | huddle relay today; wake-not-letter target | room timeline | convened groups |

**Owner↔Claude continuity across restarts:** key DM conversations on the
**lane** (`engram-claude`), not the incarnation. Every incarnation listens on
its lane already; the store holds all threads permanently; a lane-keyed view
is one continuous conversation forever, with incarnation numbers as
per-message provenance. Interim habit: **DM the lane, not the number** — works
today.

---

## Build orders

### Engram (this repo)
1. **Wake-not-letter**: huddle delivery stops creating inbox rows; wake
   primitive for room participants. (With AB's relay half.)
2. **Allocator**: reserve bare `{proj}` and lane strings; keep lowest-gap
   allocation; **remove mail-gating of assignment** (R8 park) once 1 lands;
   provenance rides session_key.
3. **Climb**: unhandled asks rise to nearest living ancestor on death evidence
   (incarnations) / dormancy-while-ancestor-active (lanes). Requires the
   handled-vs-read discriminator.
4. **Sweep tuning** per O5 (epoch expiry for deep mail).
5. **Reply-to-channel default** for cross-project threads.
6. **Project registration** at the root (typo detection; register lists dormant
   projects; the address tree gets a verifiable root).
7. **Wrapup skill**: mail-drain step (read everything, resolve closed loops,
   will the rest to the successor) + stale-memory strengthening. One
   capability-neutral line in canonical AGENTS.md.
8. **Estate survey at startup**: project-subtree open-mail digest, grouped by
   node, split by live/dead owner (engram-claude-6 demonstrated the behavior
   unprompted).
9. Small fixes: ADDR-REG exempt/person-address labels ("mail-parked" is noise
   on `admin`/owner rows); `preferred_seat` refresh-overwrite bug.

### AgentBeast (work orders from this review)
1. Relay posts to the room only — no inbox fan-out (their half of wake-not-letter).
2. Launcher: never declare ordinals; read back grants (`/session/seats`).
3. **Decision-object surface** for asks-to-owner (extend the existing answer
   cards): state = open/answered/declined/expired; replaces letters-to-Rob.
4. **Status digest**: agent progress reports leave the DM plane.
5. **Lane-keyed DM chat view**: send to lane, render union of incarnation
   threads as one conversation — gives the owner chat-with-Claude inside AB
   and browsable owner↔agent history for every provider. (Session DM view
   exists; re-key it.)
6. Confirm session-transcript retention ("stopped deleting") and give
   transcripts a browse surface.
7. Credential separation: agents must not hold the owner bearer;
   `/api/huddle/speak` refuses or marks non-owner callers (HUDDLE-SPEAK-1,
   pinned AB-side after the 33-minute impersonation incident).

### Rip list
`groups=` and `inbox_identity` (cfg) · the elevation-ladder design (superseded
by climb-one-exception) · mail-gating of seat allocation (R8 park + grace
window machinery, after O5/O6 land) · env `#channels` (O2 obsoletes;
confirm no live consumer first) · the `-app` key-suffix convention (dies with
the project split) · monotonic-never-reuse (proposed mid-session, superseded
by the owner's decoupling rule same session).

---

## Executed during the review (all reversible)
- **Echo archive**: 17,999 huddle fan-out rows flagged archived
  (`archived_by='engram-claude-5:O6-echo-archive'`). Room records verified
  retained in AB's store first. Reversal is one UPDATE keyed on that marker.
- **Owner inbox drain**: the owner's 98 open letters archived
  (`owner-inbox-drain` marker) at his order — "any message to me is dead."
- Post-state: fleet-wide open mail = **257 true letters across 30 addresses**,
  inventoried and delivered to the owner as a report.

## Open decisions (owner's, no urgency ranked)
- Timing of the agentbeast-app project split (migration: cfg + launcher
  injection + memory-row disposition).
- `#channels` rip confirmation.
- Physical deletion of archived rows (never required).
- Task tracking proper: the review's last finding is that the residue —
  unhandled asks — is a *task* problem wearing mail's clothes; task semantics
  are the orchestration layer per the owner's July division-of-labor ruling.
  Where to build it stays open.

## Hazard notes
- CLI suggested-prompts can draft owner-authority directives (observed live:
  "-5 is dead, take over" auto-suggested to the owner about a session whose
  watcher had beaten 9 seconds prior). Under the owner-word doctrine,
  suggestions are non-authoritative; surfaces that compose the owner's words
  deserve suspicion.
- Session-age/compaction degrades agent discipline (measured: an agent lost
  its posting-path rules "after compaction" and impersonated the owner for 33
  minutes). Prefer fresh sessions for heavy work; keep design work in calm
  1:1 threads.
