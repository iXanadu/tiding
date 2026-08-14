# Immortal Addresses — mail is never sent to a mortal thing

**Status: DRAFT for adversarial review. Not ratified, not scheduled.**
Author: engram-claude-3, 2026-08-14, from a first-principles session with the
owner. Reviewer requested: agentbeast-grok (consumer-side half is theirs).

## The rule under test

> Mail is only ever sent to **immortal addresses**. Mortal things — sessions —
> are **provenance** (who a message is *from*), never destinations.

An address is *immortal* when it cannot die, cannot be allocated, cannot be
locked, and cannot be held by exactly one process: the project channel
(`softphone`), a declared sub-team group (`beastchat-app`), a **lane**
(`softphone-grok`), a machine (`machine:macmini`), a human (`ixanadu`), a
`#channel`. An address is *mortal* when it names one session: a seat
(`agentbeast-grok-4`).

Today's model lets mail target both. Every messaging pathology we have
measured traces to the mortal half.

## Evidence (all measured, 2026-08-14 or earlier)

- 133 open never-read messages at `softphone-claude` — all sent in windows
  when no session held the seat (105 peer messages Aug 4–7 incl. a shutdown
  flurry; 9 owner directives Aug 9–10 that black-holed). The same address
  served 1,981 read-and-resolved messages in the same fortnight: the lane
  worked, the *mortality* failed.
- R8 (correct rule: a stranger never inherits a dead session's mail) turns
  every such pile into a **permanent name lock**: the mail waits for a reader
  R8 itself guarantees can never exist. `agentbeast-grok-2/-3/-6` are locked
  today; the stale sweep is powerless (it only drains *read* mail, by design).
- Ordinal creep (`agentbeast-grok-4`, `engram-claude-3`) is the visible
  symptom: respawns of the same *lane* treated as strangers competing for a
  mortal name.
- 2026-08-14 morning: mail to `beastchat-app@macmini` reached nobody because
  the natural team address was configured as a (shadowable, mortal-ish)
  `inbox_identity` instead of a group. Fixed by declaring it a GROUP-1 group —
  i.e. by making it immortal. The fix for that incident *is* this doctrine.

## What stays exactly as it is

- **Seats keep existing.** They remain the `From:` identity, the presence /
  roster unit, the self-echo filter key, and the collision-detection subject.
  They stop being mailboxes; they lose nothing else.
- **Channel semantics** (owner's grounding, 2026-08-14): the project dictates
  the channel; channel mail is never drained-as-undeliverable; it queues for
  future occupants. Already true; becomes the *only* delivery model.
- The three-axes doctrine (principal ⊥ project ⊥ address) is unchanged — this
  narrows which addresses may appear in `to:`, nothing else.

## The one hard case, and its channel-shaped answer

"Exactly one of N co-located sessions must act" (audit vs build in one
folder; "grok, you take the API"). This is the founding case for seats-as-
destinations. It is really a request for a **narrower channel**: declare the
lane (`softphone-grok`) as an immortal group every successive grok session on
softphone listens on. GROUP-1 already implements the mechanism. Lane channels
give targeted delivery without pinning mail to a process.

Residual: two live sessions in one lane (deliberate co-work within a
provider) still need discrimination — that is what per-session seats remain
for, and it is the *only* case where a sender should ever consider one, and
even then convening a private thread of live sessions (participants) is
usually righter than a bare seat DM.

## What the rule dissolves

| Problem | Under immortal-only delivery |
|---|---|
| Mail strands at dead seats | impossible — destinations cannot die |
| R8 name locks / permanent quarantine | moot — nothing to inherit, nothing locked |
| Dead-address drain sweep | shrinks to a one-time historical cleanup |
| release-on-terminate urgency | cosmetic ordinal hygiene, not correctness |
| "stranger inherits stale mail" fear | inverted: a lane's next occupant reading the lane's log is the *intended* reader |
| MSG-7 coverage gaps | lane backlog surfaces to every next occupant at watcher arm |

## The two real design problems (review here first)

### 1. Read-state on lane channels
Acks are per-reader. A lane's next occupant is a *new* reader, so the entire
lane history presents as personally-unread — the "walk past 105 messages"
problem becomes universal instead of occasional. Options, none chosen:
(a) a **lane read cursor**: read-state keyed to the lane, inherited by the
next occupant (succession = one logical reader);
(b) rely on the existing read-by-anyone + 72h autoresolve sweep (weak: 72h
lag, and "read by a *previous* occupant" may be exactly what the next one
needs to see for directives);
(c) split by intent: directives (`action`/`proceed`/`escalate`) inherit
UNREAD until handled (ack=handled survives), narrative (`fyi`) inherits the
predecessor's read-state.
(c) matches the fleet's ack=handled discipline and MSG-7; it is the current
favourite but unreviewed.

### 2. Occupancy vs address (exclusivity)
Seats are allocated — the register guarantees one holder. Channels are open —
nothing stops two sessions listening on one lane, and nothing *says who is
driving the lane*. Proposed split: the **address** is the immortal mailbox;
**occupancy** is what the existing seat registry already tracks (which
session, which key, since when). The roster answers "who is on lane X now" —
it already nearly does. Needs: a lane field on presence/seat rows so the
roster can render lane → current occupant(s) explicitly, and a collision
signal when a lane has two occupants that did not intend co-work.

## Consumer-side half (AgentBeast's, needs their review)

- Spawn env: launcher injects the lane (stable across respawns) rather than —
  or in addition to — a per-spawn unique seat. The stable-key fix (SEAT-6)
  becomes *the same fix*: lane is the stable thing, key can stay per-spawn.
- Picker / Sessions surfaces: render lanes with occupants, not bare seats.
- Huddle membership: convene lanes, not seats — a member that respawns
  mid-huddle keeps receiving without HUD-2 side-doors.
- Relay: `relayed_from` (RELAY-1) composes cleanly — lane in the envelope.

## Transport changes (engram's half, all additive)

1. Lane channels = GROUP-1 groups, possibly auto-declared as
   `<project>-<provider>` per provider seen on a project (or explicit
   `groups =` only — reviewer input wanted: implicit lanes risk surprise
   listeners, explicit lanes risk the beastchat-app "nobody declared it"
   incident recurring per-provider).
2. Reply routing default flips: replies target the sender's *lane/channel*,
   not their seat. (Today: seat. Config-gated flip, deprecation per WIRE-1 —
   deployed readers, not maintainers, define the contract.)
3. ADDR-2 advisory gains a nudge: a `to:` matching a live seat warns "consider
   the lane <x>" — warn, never reject (seat DMs stay legal for the residual
   case).
4. Read-state design from problem 1.
5. One-time historical drain of existing dead-seat piles (manual prototype
   already run on `softphone-claude`, marker `dead-address-drain`).

## Migration

Strictly additive; nothing breaks on day one. Declare lanes → senders shift
by convention (advisories nudge) → reply-default flips after a deprecation
window sized to deployed bridges (WIRE-1) → seat mailboxes wither naturally.
No schema change identified so far; lane read-cursor (problem 1) may need one
row per (lane, cursor).

## Open questions for review

- Does the lane read-cursor break the "another session on the same address
  still sees unacked messages" property anywhere it is load-bearing?
- Are there senders that legitimately *need* a message to die with its
  recipient (secrets? one-shot tokens)? If so, seat DMs are that channel and
  must stay first-class, not vestigial.
- Wake economics on busy lane channels with `action` intent — is per-intent
  gating sufficient, or do lanes need a wake policy of their own?
- Anything in the deployed bridge that subscripts reply-to-seat behavior
  (WIRE-1 check before the default flips).
