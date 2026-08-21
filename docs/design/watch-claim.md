# Watch-Claim: one seat, one watch, no ritual

Status: DESIGN FOR ADVERSARIAL REVIEW — not built. Owner GO 2026-08-20 ~22:10Z.
Decision record: project memory `decision/wake-architecture-direction-2026-08-20`.

## The problem, in one measured line

Same seat, same sender, identical ask: **50 minutes** to answer without a
watcher, **under 2 minutes** with one — and the 50-minute reply arrived only
because the fix reached back and delivered it. Mail that arrives and then
waits for something else to happen.

Watcher arming today is an agent-performed startup ritual. 2026-08-20
produced its complete failure catalog: never armed (WATCH-G1), believed-
armed-never-ran (a `ps` check matched a NEIGHBOR's watcher), armed-then-died
(SEAT-13), armed-with-wrong-identity, armed-twice (double wakes). Plus the
economics: agents spending model turns BABYSITTING watchers overnight
(~400-750k tokens per wasted turn, full context re-sent each time).

## Invariants (owner constraints — violating any of these fails review)

I1. Manual CLI spawning cannot be prohibited. The sessions spawned manually
    (engram/agentbeast/maintenance) are spawned precisely when infrastructure
    is broken. The repair crew must not depend on the thing being repaired.
I2. AB death breaks huddles only. Bare mail transport survives.
I3. Engram/Tiding is fully useful WITHOUT AB (adoption/standalone).
I4. One watcher per seat, enforced structurally — never by convention.
I5. Idle costs zero model tokens. Supervision is process-level, never a turn.
I6. The register never claims more than it observed (delivered ≠ seen;
    watch-held ≠ delivery-working — state both honestly).

## Separation that makes it simple

**SENSING** (poll, claim, beat, restart) — the bridge owns it, universally:
all four CLIs (claude/codex/cursor-agent/grok) auto-spawn the bridge from MCP
config. That is the one prose-free hook that exists everywhere.

**DELIVERY** (mail-arrived → agent turn) — per harness:
  D1. Monitor-type tool attached (claude, grok: proven 49s) — push.
  D2. AB user-turn injection for AB-hosted sessions — push, zero agent acts.
  D3. Banner on next tool call — the floor, everywhere, always.

AB exits the ARMING business (its WATCH-G1 armer stands down naturally via
the claim protocol, below) and keeps only what the spawner alone can do:
injection where no Monitor is attached, and idle+unread nudging.

## The claim protocol

A watch is a single-holder claim on a SEAT, stored server-side like a seat.

Row: `watch/<seat>` — metadata {nonce, pid, host, armed_by: bridge|ab|agent,
claimed_at, last_beat}.

- **Claim**: `POST /session/watch/claim {seat, nonce, pid, host, armed_by}`
  → `granted` | `held {armed_by, last_beat_age}`. Grant iff no row OR the
  row's last_beat is older than EXPIRY. DB unique key on `watch/<seat>`
  settles same-instant races; loser sees `held`.
- **Beat**: the watcher already beats each poll (~45s). The beat now carries
  its nonce and the response carries a verdict: `holder` | `displaced`.
  Poll cycle order is **beat → (exit if displaced) → fetch → emit**, which
  bounds double-emission during a takeover to ≤ one cycle.
- **Refused/displaced = print why and EXIT.** Never standby (standby is the
  zombie class with better manners).
- **EXPIRY**: 3 missed beats (~150s). Server clock only (`NOW()`), no client
  clocks anywhere.
- **Release** on graceful exit; crash is covered by expiry. Self-healing
  costs zero turns (I5).

Free wins: "already armed?" becomes a server query (kills the wrong-subject
`ps` probe class at the boundary); the deaf-flag is just "seat with no live
watch" read off the claims table; existing doubles converge at next beat.

## Who spawns, per harness (v1)

- **claude / grok (Monitor exists)**: v1 keeps the agent launching the
  watcher **under Monitor** — but the launch collapses to ONE mechanical act
  with zero decisions: a bridge tool returns the exact blessed command
  (paths, identity, project-dir all resolved bridge-side); the watcher then
  CLAIMS, so double-arming is impossible and the banner reports unheld
  watches. v2 (open question for review): bridge spawns the watcher as its
  child writing to a per-session FIFO; the Monitor command becomes a
  streaming reader of the FIFO (a `cat` loop — NOT `tail`, see §Consumer
  below) — supervision moves fully bridge-side and the watcher
  dies with the bridge (kills SEAT-13 orphans by process lineage).
- **AB-hosted grok**: AB's existing waiters adopt the claim. First to claim
  holds; the other path finds `held` and does nothing. Migration measures
  itself: when AB's armer hasn't won a claim in a month, delete it.
- **cursor**: today covered by NOTHING (WATCH-G1 shipped grok only). Same
  claim rules; which spawn path works is part of the live test.
- **codex (KNOWN WRINKLE)**: the bridge is daemon-scoped — ONE bridge, many
  seats. Bridge:session ≠ 1:1. v1: codex bridge arms nothing; pinned
  follow-up designs per-seat watchers from the shared bridge. Do not let
  codex block the 80%.

## Failure modes table (attack these)

| # | failure | answer |
|---|---------|--------|
| F1 | two watchers race the claim | DB unique key; loser exits with reason |
| F2 | holder dies silently | expiry (~150s) frees; next arrival claims; no turns spent |
| F3 | holder stalls (load), then resumes | its next beat says `displaced` → exits; ≤1 cycle double-emit window |
| F4 | **squatter**: claims + beats but never delivers | worst case. Register would say covered while nothing arrives. Partial mitigation: `armed_by` recorded; delivery observable via structural `delivered_to` after wakes; a seat whose watch beats but whose mail is never delivered within N polls is flaggable. NOT fully closed — reviewer input wanted |
| F5 | claim API down | watcher runs unclaimed? NO — fail-loud, retry with backoff, banner says unheld. Unclaimed watchers are the pre-protocol world back again |
| F6 | clock skew | server time only |
| F7 | seat moves (rename/regrant) | watcher re-reads seat file each poll (existing behavior); claim keys on the CURRENT seat; old claim expires |
| F8 | channel mail, two seats one project | watch is per SEAT; both watchers wake — correct, both sessions should hear the channel |
| F9 | AB and bridge both arm at spawn | whoever claims first holds; other exits; no coordination needed |
| F10 | **PARTIAL COVERAGE (wild specimen, 2026-08-20 22:14Z)**: a BARE watcher (no seat identity) resolves channel+lane only — seat-addressed DMs never wake it, while every liveness probe passes. Its own estate survey even LISTED the live seat it wasn't listening for | claim must record the LISTEN SET, and the claim is REFUSED as `partial` when a live ordinal seat for the project is absent from it — a partial watch is not a watch. The watcher can detect this itself (it prints the survey); the server can verify it (it grants the seats) |
| F11 | **CONSUMER PARSES EVENT LINES AS MESSAGES (wild, same night)**: watcher stdout mixes `{"event":...}` lines (backlog-digest, estate-survey) with `{"id":...}` message lines; AB's wake loop woke sessions on the event lines and published it as proof of delivery | make the discriminator a stated CONTRACT: wake-worthy lines carry `id`, advisory lines carry `event`, nothing carries both; ship a reference consumer snippet; the watcher could also gain `--messages-only` for consumers that cannot filter |

## Migration and prose retirement (order is load-bearing)

1. Ship claim protocol (server) + claim-aware watcher + blessed-command tool.
2. Arrival-matrix rows, cold, per harness: fresh session, prose step
   DELIBERATELY SKIPPED — does a watch get claimed? does a DM wake it?
   (Owner tests cursor/grok live.)
3. Only then cut the prose (startup skill 4b, AGENTS.md auto-watch, tonight's
   broadcast recipe) in one pass, superseding not deleting. Removing prose
   before step 2 passes re-opens the tagApp hole by documentation (I1).

## Explicit questions for the adversarial reviewer

R1. Break the claim protocol: find a state where two live watchers both
    believe they hold, or where no watcher holds and the register says one does.
R2. F4 (squatter/false-covered): is the mitigation real or theater?
R3. v1-vs-v2 on claude: is agent-launch-under-Monitor + claim good enough,
    or does the FIFO design pay for itself immediately?
R4. Expiry at 150s: too tight (load-stall displacement churn) or too loose
    (3-minute deaf windows after crashes)?
5. The codex deferral: acceptable, or does deferring it recreate WATCH-G1
    for codex the way claude-only WATCH-A4 did for grok?
R6. What does this design silently assume about AB that violates I2/I3?
R7. F10's `partial` refusal: right call, or does refusing a channel-only
    watch make things WORSE for sessions that genuinely have no seat
    (admin, watchers on seatless projects)? Where is the line?

---

# v2 — POST-REVIEW REVISION (adversarial cut: agentbeast-app-grok-2, 2026-08-20 22:47Z)

Verdict on v1: **do not build as written.** Three S1 kills accepted in full.
v1 text above is retained for the record; where v2 conflicts, v2 governs.

## Kills accepted

K1 (was F9/exit-forever): **AB never holds the sensing claim.** An AB-held
watch dies with AB and the exited losers never return — mail death with AB,
violating I2, structurally. Sensing lineage is bridge/session-side only.
D2 injection is DELIVERY, not a watch. And **refused/displaced launchers
RE-CLAIM on a timer** (process-level, I5-safe) — exit-forever was wrong;
exit is right only while a successor is actually beating.

K2 (was F4 mitigation): **exclusive claim must not let a mute holder lock
out a working deliverer.** v1's after-the-fact flag was theater. v2:
delivery-liveness displacement — a holder that has emitted nothing across N
polls IN WHICH MAIL ARRIVED for its listen set is displaceable; and the
sensing lock never gates D2.

K3 (was F5): **I1 wins.** When the claim API is unreachable, a manually
launched watcher RUNS UNCLAIMED, loudly marked UNHELD on stderr and in its
own banner, and never shown as covered in the register (I6). The repair
crew hears each other while the store is sick.

## Protocol corrections

P1: **Beat and steal are one nonce-CAS statement**
(`... WHERE nonce=$expected OR last_beat < expiry`), copied from
seat-claim's tested pattern. A beat whose response is lost = holder-unknown
→ STOP EMITTING until a verdict. Nonce is RANDOM, never pid (pid reuse
inside expiry reincarnates ghosts).
P2: claim row carries **project_dir + listen_set**; mismatch on either →
displace. (Kills the neighbor's-watcher class server-side; subsumes F10.)
P3: expiry **150s**, floor 90 / ceiling 180. Stampede after a store outage
is bounded by the one-fetch takeover rule — TESTED against a real 8920
bounce, not asserted.
P4: **a new holder's first act is fetch-unread-and-emit** — gap mail during
an expiry window must never depend on a side path reaching back.
P5: D3 (banner) is NOT delivery coverage — it serves already-awake sessions
only. Coverage math counts D1/D2 firing, nothing else.

## Spawn path: v2 only

v1's "blessed command" is a shorter ritual, not a prose-free hook — 0/4
grok sessions performed the one act tonight; the step-2 arrival matrix
fails it by construction. Ship the bridge-child + FIFO + streaming-consumer design
directly, with the two lessons priced in: **reconnect re-claims (no
skip-guard — AB's "surviving process holds its monitor" comment was the
bug), and the claim is not taken until the tail consumer is attached**
(else the FIFO is F4 with extra steps).

## Coverage holes, named with dates — not deferred silently

- **codex**: shared bridge claims one watch per seat it serves, minimum,
  or I4 is false on day one. Named hole, owner-visible, date attached at
  build time.
- **cursor**: covered by NOTHING today. A spawn path ships in the same arc,
  or this is WATCH-G1-for-cursor within the week.

## Scope honesty

Watch-claim does NOT fix the huddle wake noise (letters-off posts never
touch the inbox waiter; D2 injects independently). That fix is the
mention-directed + coalesced-wake proposal, separately owned. One sensing
watch + AB injection can still mean two wakes per huddle post until that
lands.

## Build order (reviewer's, adopted verbatim)

1. nonce-CAS claim+beat, tests copied from seat-claim incl. expiry-steal
   vs in-flight beat
2. v2 spawn (bridge child + FIFO + tail) with reconnect re-claim, no
   skip-guard
3. AB does not claim (their armer retires from sensing)
4. unclaimed-allowed when store unreachable (UNHELD, loud, never covered)
5. delivery-liveness displacement / D2 never gated by the sensing lock
6. arrival matrix: prose skipped, AB killed, store bounced. **If step 6
   fails, the protocol is theater too.**

**Store-bounce leg: MEASURED CLOSED, 2026-08-20 23:54Z.** During the
9a806f2 prod restart a session-owned waiter (agentbeast-app-grok-2's pid
84246) printed `poll error (All connection attempts failed); retrying`,
then reconnected — same pid, no re-arm. Combined with the fleet record
(AB's waiters and the engram control watcher surviving all six bounces
that day), the leg the gate could not run in-harness ran itself in
production, on every waiter class, and passed. Witness: app-grok-2's
Monitor capture, huddle DfNRCl6x.

Rider on step 6 (reviewer, accepted): a matrix row is a FAIL if it shows
only project/lane wakes — it MUST include an ordinal-seat DM, the address
AB cannot name today. Channel-only green is the coincidence already paid
for once (the 21:57 "proven"). Step 3 (AB sensing retirement) executes
PER SEAT, only after that seat's row — including the seat-DM leg — is
green. AB's D2 injection (huddle/chat turns) is delivery, not sensing,
and is not part of any retirement.

## Consumer: a cat-loop, never tail (measured 2026-08-21)

The shipped hint said `tail -F <fifo>`. It is wrong on every platform we
run. `tail` on a non-regular file reads to EOF before printing (it has to,
to find "the last N lines"), and a FIFO whose writer is alive never hits
EOF — so every wake sat in tail's buffer while the store read the seat as
`covered` (the claim follows the attach, and tail *had* attached: fd open,
bytes consumed, nothing emitted). The owner found it by DMing the engram
session and getting silence; the acceptance gate had read the FIFO with a
Python `O_NONBLOCK` reader and never exercised the command the hint hands
to agents — a two-clause claim with one clause measured, again.

Measured on macOS (`tail -F`, `tail -f`, `tail -n +1 -f`: all silent until
writer close; `cat`: live, but exits on writer close; `while true; do cat
FIFO; done`: live AND survives the watcher restarting). The hint now emits:

    while true; do cat <fifo> 2>/dev/null; sleep 1; done

The loop is what `-F` was for (reopen after the writer's end recreates);
the `sleep` keeps a missing FIFO from spinning. `memory_status` prints the
exact command. Covered by `test_watcher_attach_command_streams_live` in the
bridge suite, which runs the hinted command for real against a held-open
writer — the test that would have gone red on `tail`.

## Consumer loss is EPIPE, not a stall (measured 2026-08-21 11:53Z)

This document said above that if the reader detaches "writes block, the
poll loop stalls, beats stop, and the claim EXPIRES — honestly." Half
right. Beats do stop and the claim does expire — but not because writes
block: a write to a FIFO with **no reader is `EPIPE`**, and the shipped
watcher took it as a crash. Sequence, from engram-claude-2's transcript
and wake log: the session's Monitor cat-loop died (cause in the harness,
still unexplained — its input queue also froze for 6.5 minutes); the next
emit raised `BrokenPipeError`; the dying gasp went down the same dead
pipe; the supervisor respawned a child that sat at open-for-write with no
claim; the seat read `expired`; the owner typed at a session that believed
itself covered.

Repaired contract (`_out`, `_open_fifo_for_write`, `_orphaned`):

- **Emit survives detach.** On `EPIPE` the watcher logs, re-opens the
  FIFO (waiting for the next reader), and re-sends the line that failed —
  the wake that found the consumer gone is the first thing the next
  consumer sees. While it waits, beats stop and the claim expires: still
  the honest answer to "nobody is listening." The first beat after
  re-attach re-asserts the claim (same nonce, CAS on the row) or learns it
  was stolen and exits for respawn.
- **The open is interruptible.** `O_NONBLOCK` open polled once a second,
  switched back to blocking once a reader exists, so a watcher whose
  bridge has died (`getppid() == 1`) exits instead of becoming the
  WATCH-CLAIM-3 orphan. The poll loop checks the same thing and releases
  its claim on the way out.
- **The session is told.** The bridge senses `GET /session/watch/status`
  on its heartbeat (briskly while uncovered) and prepends
  `⛔ WAKE STREAM NOT COVERED` with the attach command to every tool result
  until the seat reads `covered`. Claude-only: the command never returns,
  so a harness without a background stream tool is never handed it.
  `memory_status` now MEASURES coverage at the store rather than assuming
  it from the existence of a FIFO.
- **The gasp never hands over a launch.** A bridge-spawned watcher's last
  word names the attach command, not `engram-inbox-wait`; the legacy form
  says "do not re-arm — memory_status and attach."

Proven live the same morning against the real store: reader killed →
emit into the void → watcher alive, blocked → second reader attached →
the lost wake delivered first. Unit coverage:
`tests/test_wake_stream_consumer_loss.py`.

## Prose retirement, executed (owner order 2026-08-21)

"Engram spawns watchers, agents never do." Every instruction that had an
agent launch `engram-inbox-wait` is gone from the startup skill, the global
agent directives, the README, SECURITY, messaging, multi-provider and
daily-workflow docs, and the watcher's own dying gasp; the remaining legal
use of the console script is as a launcher-owned reader/consumer (AB's grok
path, WATCH-G1) and in the acceptance harness. WATCH-CLAIM-2(a) — "retire
prose only after the per-harness matrix passes cold" — is overtaken by the
owner's ruling: the believed-armed hole is now closed by the banner, which
is code, not prose.
