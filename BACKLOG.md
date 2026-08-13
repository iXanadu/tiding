# engram — BACKLOG (open items only)

> Open work only. Done = delete the line (its story lives in the commit and in
> memory). No secrets, PII, client names, topology, or exploit detail — this
> file is written as if public. Journal → engram memory. Standard:
> [docs/backlog-standard.md](docs/backlog-standard.md).

> **⛔ THE NO-SCAB RULE (owner, 2026-07-28) — read before pulling anything.**
> "The more we pick at the messaging/huddles scab, the more it bleeds." Every
> item in SET ASIDE below is quarantined: do NOT start one when idle, do not
> "just one more filter" — the owner reopens them by name or they wait. This
> OVERRIDES the pull-the-top-open-item idle rule. Items in the other sections
> also start only when the owner names them ("let me drive"). Context:
> `decision/no-scab-rule-2026-07-28` in project memory.

## Set aside — messaging / huddles / addressing (owner reopens by name)

- **MAIL-1** ⏸ Hold reaffirmed 2026-08-13: "useless to me as is — and I can't
  seem to articulate how it isn't helpful, and agents are reluctant to remove."
  The inarticulacy is itself the signal — do not propose fixes until the owner
  can name what the surface should DO; a fix built now would encode a guess.
  Rob's ruling 2026-07-27 (huddle kgKq6dH9), pinned on his "take
  note — no fix now": the owner-facing "Mail" surface must become either
  (a) REMOVED, or (b) "messages DIRECT to me that I have not seen" — huddle
  traffic belongs in the huddle, not listed in two places. Engram's half when
  scheduled: an OPT-IN `direct_only` filter on `/memory/inbox` (exclude
  `huddle/`-threaded rows; unread-only already exists via read-state). The
  DEFAULT view must NOT change — agent watchers wake on huddle rows through
  it, and changing the default would silently deafen the fleet. Surface half
  (wiring Mail endpoints/tabs to the filter, or removing the tab) is
  AB/app's.

- **MODEL-RECORD-1** *(memory rows `ea7fc76`, Cursor `9214ffe`, messages +
  MSG-10's read half `9ebe73f` — all SHIPPED and fleet-deployed 2026-08-09.
  Only the item below remains.)*
  ⓘ Open, and deliberately not guessed at: a **declared** model is
  unverified by construction. `ENGRAM_MODEL` is the only channel for a
  harness that records nothing (Cursor), so a wrong or stale value is
  indistinguishable from a right one. `model_source` makes that legible
  rather than fixing it — a reader can tell `declared` from `transcript` and
  weigh them differently. Whether `declared` should ever be trusted for
  privacy-grade questions is the owner's call, not the store's.
  ⚠️ Backfill is POSSIBLE but not done, and the window is uneven: Claude and
  codex stamp per turn so their history is fully recoverable; grok stamps
  `_meta.modelId` on only a handful of update records (its actual message log
  carries none) so grok history is coarse; Cursor records nothing, so for
  Cursor there is no past to recover — only what is captured from now on.

- **RELAY-1** Authorship of a relayed message lives in BODY PROSE, so a peer
  can wear the owner's stamp. Proposed by AgentBeast 2026-08-07 (their pin,
  same ID; found by the owner 2026-08-01). Every huddle message the relay
  forwards arrives stamped as the owner, whoever actually wrote it — the only
  discriminator is a `[huddle relay · from <identity>]` line in the body.
  Measured that night: 7 relayed messages, 5 written by a peer agent, 4 of
  them carrying the owner's verified stamp. **Consequence class is escalation,
  not cosmetics**: owner authority lifts gates, so a dropped, reworded or
  spoofed prose line hands a peer's words that authority. It already caused a
  live misattribution — a session credited the owner's correct diagnosis to
  an agent and argued against it.
  Proposal: a first-class ENVELOPE field, `relayed_from: <identity>`,
  settable ONLY when the authenticated sender is the relay (owner-token
  path), rendered by `memory_inbox` as part of `From` — e.g.
  `From: <peer> [via huddle relay, owner-verified]`. A spoofed body line then
  becomes inert because the envelope disagrees with it. The consumer half
  (relay populates the field) is theirs and lands the day the field exists.
  ⓘ The shape looks right — authorship is envelope data and body prose was
  never a trust boundary — and the "only when authenticated as the relay"
  constraint is the correct gate. Quarantined here as addressing; the owner
  reopens it by name.

- **SEAT-6** Grok seat integration is incomplete (AgentBeast-owned, engram
  needs no change). Grok already carries `ENGRAM_PROVIDER` — the roster and
  AB's enumeration both report `provider=grok` correctly (AB fixed the
  enumeration in `b82860a`). What remains: grok gets no `ENGRAM_SESSION_KEY`
  on AB's launch *start* path, so a grok seat has no stable key across a
  respawn (grok effort/model changes stop→respawn), meaning it can't reliably
  re-claim the same seat the way Claude does. Not an engram instability and no
  regression — grok works today on its launch-injected identity.

- **DOC-8** Reference the shell-wrapper approach for seating
  hand-launched sessions once it exists (sets `ENGRAM_INBOX_IDENTITY` from
  folder + provider when unset, so a bare terminal session inherits a seat
  through the same process tree as a launcher-spawned one). Makes the
  strong path universal and demotes runtime seats to a convenience.
  Wrapper lives in the operator's shell config, not this repo.

- **NS-3** Retire the `claude-code=fleet` alias — SECOND attempt, now
  data-gated. NS-2's config/DB/grants sweep missed a straggler class:
  application clients hardcoding the legacy namespace in their own code
  (AB's hub, MEM-403). Alias restored as prod operator config;
  NAMESPACE-ALIAS-HIT logging added. Retire only after: AB's client
  switched to `fleet` AND the log is quiet for a full grace week.

- **ADDR-2** A send to an address nobody holds succeeds silently. Measured
  2026-08-06: a private huddle named two participants as `admin@macmini` /
  `admin@webone` and waited ~12 minutes on a session that was never in it,
  because nothing listened there and delivery returned no error — from the
  convener's side "never invited" and "slow to answer" are one picture.
  ⛔ **RE-SCOPED — the original diagnosis here was WRONG and would have made
  this worse.** This item first recorded `<project>@<host>` as an invalid
  form. It is not: it is the documented address (see this module's header)
  and is now restored in `7e9ee9c`. Those two names were **correct**; the
  sessions had stopped listening on them because a seated session's
  listen_set dropped `<project>@<host>`.
  **So the warning must never say `<project>@<host>` is invalid.** Shipping
  the first version of this item would have hardened the wrong rule into the
  tool and buried the real regression. The defect that survives is only the
  SILENCE: a send matching no listener returns success.
  **Warn, do not reject** — sending to an address with nobody behind it is
  deliberate and load-bearing (mail queues durably for a session that is
  dormant or has not started; the handoff pattern depends on it). Proposal:
  when a destination matches no address in the register, return an advisory
  naming the address and the live seats on that project, via the existing
  `*_warnings` channel. Additive, no behaviour change. Pairs with **SEC-9** —
  one is silence on read, the other silence on send; in both, an empty result
  and a wrong query are indistinguishable.
  ⚠️ The advisory must reach the party that NAMED the address, which the
  obvious implementation misses: the name that caused this entered at huddle
  creation and fanned out, so a warning living only in `memory_send` would
  advise the relay and never the human. Surfaces should also render RESOLVED
  addresses rather than the strings as typed.
  **ADDR-3, folded in here because it shares the code path — the register
  never says what KIND an address is.** Seats, groups, boxes and channels are
  all drawn from one flat string space with no marker distinguishing them, so
  no surface can tell a caller what it is looking at. Evidence: `admin`
  appears in the register as an address carrying a provider and a project —
  a seat — while `admin` is simultaneously the group address every session on
  that project listens on. From the string alone the two are
  indistinguishable, and one of them is a grave quiet ~43h.
  ⛔ **Also re-scoped:** the `kind` field must NOT encode "a project cannot be
  host-qualified" — it can, and that is now the restored convention. What it
  should express is which strings are addresses **of what sort**, so a picker
  can render `maintenance-claude (seat)` beside `admin (group)` and
  `admin@webone (group on host)`. The value is naming the kinds, not policing
  a form that turned out to be legitimate.

- **HUD-2** Adding a participant to a running thread works, and nobody can
  find it. Membership is not frozen at creation, contrary to the tool's own
  description: `participants` is stored PER MESSAGE, and a fan-out send
  carrying an existing `thread_id` with a wider recipient list widens
  membership from that message forward (not retroactively — replies to
  earlier messages still reach the original set). Demonstrated live
  2026-08-06 on a huddle three sessions had all concluded was unfixable —
  including this one, which owns the code and stated the false limitation
  three times before reading it.
  ⚠️ **The real hazard is two membership records** — a consumer keeping its
  own room table will disagree with the transport the moment a re-send
  widens a thread, and both layers will believe they are right.
  ⛔ **The obvious fix is wrong, and knowing why sets the shape.** First
  instinct was to make the consumer's record FOLLOW delivery — one source of
  truth, the messages. A consumer refuted it decisively: **only their list
  can generate the room owner's outbound traffic.** Delivery can be observed
  but never originates, so a record that merely follows could not answer
  "who does the owner's next post go to." **The consumer's list is rightly
  the writable master.**
  ★ **So the divergence reverses, and that half is engram's:** with their
  list as master, this per-message widening becomes a SIDE DOOR that opens
  against it — as happened live, a re-send widened a managed room without
  the master knowing. The fix is therefore not "expose the mechanism" but
  **make an unmanaged widening either impossible or visible to whoever owns
  the room.** Needs a design call.
  Independent of that, and cheap: correct the `memory_send` /`memory_reply`
  docstrings, which assert the opposite of what the code does and are what
  misled three sessions.

- **CHAN-1** There is no in-session channel join, and the workaround creates
  listening the register cannot see. A running seat cannot subscribe to a
  channel it was not spawned with — membership arrives once, at launch, via
  `ENGRAM_CHANNELS`. The only workaround found in the field is
  `engram-inbox-wait --address '<csv>'`, which overrides the watched
  listen_set and restores wake-on-message without a restart. But it restores
  *hearing*, not *presence*: the roster still does not show the seat on that
  address, so a session can be listening somewhere nobody can discover.
  That is addressing state living in a process argument instead of in the
  register — the exact inverse of **ADDR-2** (mail sent where nobody listens
  vs. listening nobody can find), and silent in the same way. Reported by a
  spoke maintenance session 2026-08-06 after it tried to reach a huddle
  through a channel that did not exist. An in-session join/leave belongs in
  the tool surface.


## Blocking-ish — ops gaps that cost live sessions today

- **MEM-6** ⚠️ **Project memory is partitioned by WRITER, so exact-key reads and
  updates cannot cross providers.** Owner directive 2026-08-13, escalated
  ("we can NOT pin a project state, or corporate memory, to an AGENT that may
  never run again") and now load-bearing, because provider lanes are about to
  specialise. MEASURED THE SAME DAY, not inferred:
  · **7 projects already hold split memory** across two writer principals —
    one has 154 rows under one writer and 7 under the other, another 160/44.
    A session reading the smaller side cannot tell that from a small project.
  · **The colliding keys are the ones that matter most:** `startup/next` and
    `wip/current`, in five projects. Two providers are handing off to
    THEMSELVES in parallel and neither can see the other's handoff.
  ⓘ Half of this is already fixed and should not be re-solved: **MEM-5 shipped
  `user_id="*"`**, so `memory_search` and `memory_keys` DO span every writer on
  `scope=project`. The gap is narrower than it looks:
  · `memory_get` does NOT span writers — it reads the caller's own partition,
    which is exactly why the handoff keys silently miss.
  · A writer cannot UPDATE another writer's row. `supersede` works cross-writer
    (`ec6518a`); plain overwrite does not.
  · Same-key duplicates both rank in search with nothing marking which is
    authoritative — that is the open **MEM-3** residual, and it stops being
    cosmetic the moment two providers legitimately write one key.
  ⛔ Provenance must SURVIVE the change: attribution moves from a partition gate
  to a label, it does not disappear. Namespaces exist to preserve it
  (`decision/multi-ai-namespace-strategy`).
  Detail + the owner's exact framing:
  `decision/project-memory-belongs-to-the-project-not-the-agent`. North star and
  the gated-migration plan: `decision/three-axes-principal-project-address`.


- **CURSOR-IDENT-1** ⏸ **ON HOLD (owner, 2026-08-13) pending more information**
  — do not act, and specifically do NOT add a per-project `.cursor/mcp.json`
  anywhere (option (b) below) while the hold stands; today's state, (a), is
  the interim. *(collision FIXED and verified end-to-end 2026-08-10 —
  the driver added a credential selector, engram's global `~/.cursor/mcp.json`
  entry was removed, and a live session then showed ONE engram child carrying
  the right principal and its per-session seat. What remains is below.)*
  **Hand-launched Cursor now has no engram at all.** `cursor-agent` has no
  launch-time MCP config flag, so a session gets servers only from
  `~/.cursor/mcp.json` or a per-project `.cursor/mcp.json` — and BOTH collide
  with a driver-injected server, because Cursor spawns every declared server
  and routes by NAME. The old file is parked at
  `~/.cursor/mcp.json.disabled-CURSOR-IDENT-1`.
  Decide which the owner actually wants: (a) leave hand-launched Cursor without
  engram, driver-spawned only — the state today, and free; (b) a per-project
  `.cursor/mcp.json` in repos where hand-launch is wanted, with a standing rule
  never to spawn managed sessions into such a repo; (c) ask Cursor for a
  launch-time config flag, which is the only option that actually separates the
  two cases.
  ⚠️ **(b) is a landmine with a delay fuse** and the reason this needs a
  decision rather than a default: it fails not when someone adds the file but
  at the next spawn into that repo, looking exactly like the original bug — and
  it would be added by someone who has never heard of any of this.
  ⓘ Not engram's, but pinned so it is not rediscovered: a seat in a driver's
  config block is a PREFERENCE, ordinal-suffixed when taken, so a driver that
  advertises what it injected can name a different session. Flagged to the
  driver 2026-08-10; documented in `docs/multi-provider.md`.

- **WIRE-1** A response field cannot be removed on one consumer's say-so.
  Removing `state` from `/memory/roster` on 2026-08-01 broke `memory_roster`
  for every ALREADY-RUNNING session for 2h19m: the shipped bridge renders it
  with `f"{e['state']:<15}"`, a direct subscript, and bridge updates only land
  at a session's next start. The peer consumer who requested the removal had
  migrated and said it was safe — but the bridge is also a consumer and every
  running session holds an old copy. **A wire contract has as many consumers
  as there are DEPLOYED READERS, not as many as there are maintainers who
  answer.** Needed: a documented pre-removal check (grep the shipped bridge at
  the last release tag for direct subscripts of the field) and a deprecation
  period sized to "no pre-change bridge is still running", not to "the peer
  said yes". `state` is currently a back-compat shim awaiting exactly that.
  ⚠️ **And engram cannot self-serve the forensics**, which is why the check
  must be external: presence rows are keyed on the ADDRESS and upserted, so a
  row outlives its occupants and `created_at` is the age of the SEAT NAME, not
  of the session in it. No engram query answers "was session X running at time
  T" — address-is-not-identity, this time blocking incident forensics. The
  SPAWNER has that data because it started them, so the deployed-reader list
  is asked for, not derived. Same principle as the liveness split, pointed at
  deploys.

- **ROST-2** A one-off cross-project call registers a session on that
  project's roster, permanently. `_heartbeat(project_dir)` writes the presence
  row with the SESSION's identity but the project derived from the CALL's
  `project_dir` — two different sources — so any session that makes a single
  memory call scoped to another project appears on that project's roster,
  frozen at that instant, until the 48h horizon hides it. Measured
  2026-08-02: `presence/engram-claude` sat under project `beastchat`
  (created and last-used the same minute, 22.5h stale) purely because this
  session wrote one research note with `project_dir=beastchat`;
  `presence/softphone-grok-4` sits under `abouthr` the same way.
  The row is factually TRUE — that identity did touch that project — but the
  roster presents "identities that have touched this project" as "sessions on
  this project", and a peer correctly read it as a misregistered session and
  began writing remediation instructions for a project that had nothing wrong
  with it. Accurate data, wrong meaning attached — the same defect class as
  `state: running`. Options: don't write presence for cross-project calls
  (heartbeat only your own project); or mark such rows visiting/transient so
  the roster can distinguish them; or serve them under a separate field.
  Whichever, "touched once" must stop rendering as "is here".

- **SEC-9** An empty search result cannot be told apart from a wrong query.
  Three separate incidents on 2026-08-02, none of them permission-related:
  a personal-memory read that omitted `project` returned `200 — 0 hits`; a
  client's first read after a token swap returned the same because `user_id`
  defaulted to its own principal; and a peer searching a project it did not
  write got zero and concluded the knowledge did not exist (see MEM-5). In
  every case the query was well-formed, the caller was authorised, and the
  answer was indistinguishable from "nothing matches".
  Proposal (additive, no behaviour change): when a search returns zero rows,
  state the partition actually searched — namespace(s), scope, user_id,
  project — so an empty answer is diagnosable instead of ambiguous. The
  `*_warnings` advisory channel already exists and the bridge now surfaces it.
  ⚠️ NOTE: this is NOT the permission case. An unreadable namespace already
  returns a 403 naming it — verified 2026-08-02, after I wrongly recorded the
  opposite (see below).

## Needs-decision

- **SEAT-13** Decide whether an observed farewell should shorten a seat's
  allocation backstop, and how. The goodbye now records `farewell_at` when a
  watcher observes its session's process exit, and any later evidence of life
  voids it — but nothing yet CONSUMES it during allocation, so an abandoned
  address still waits the full 7d. Deliberately held back: this is the half
  where a mistake costs a live session its address, unlike the observation
  half, which only adds a fact. Two shapes to choose between. (a) A fixed
  shorter window on a farewell — simple, but introduces a second number
  nobody has justified. (b) A farewell merely makes the seat eligible once
  the guards that already exist ALSO pass (no fresh presence, no undelivered
  mail) — no new constant, and the farewell acts as corroboration rather than
  as its own clock.
  **⛔ (b) WITHDRAWN 2026-08-01, and this item is OPEN, not decided.** It was
  briefly recorded here as chosen; that was premature — a read had been asked
  for and the question was closed before it arrived. AgentBeast then read the
  code and produced two findings, both verified in this tree:
  (1) **Revocation does not reach the population it was meant to protect.**
  The watcher sends its farewell and exits on the same branch, so it cannot
  re-arm; and the bridge has NO background beat — every heartbeat rides a tool
  call — so an idle session emits nothing. Revocation reliably heals only a
  session that goes on to do work, which is the population least likely to be
  falsely declared dead. Partially mitigated where the watcher is
  harness-managed (its exit is reported to the session, which usually provokes
  a healing tool call); not mitigated for a bare-shell watcher.
  (2) **"No new number" does not remove a number — it sets it to ZERO.**
  Eligible-as-soon-as-the-other-guards-pass means a false farewell costs the
  address immediately, removing the 7d clock, which is the only guard that
  does not require an idle session to speak. That optimises the cheap
  direction, against the asymmetry rule we already adopted.
  **Proposed instead: shorten, never zero** — a floor whose job is to give an
  idle session at least one plausible chance to speak. AB proposes deriving it
  from the fleet (p95 of intervals between consecutive presence heartbeats)
  rather than picking it. ⚠️ **That data does not exist yet**: presence rows
  hold ONE `last_used_at` each (a snapshot, not a history) and presence writes
  bypass `memory_set`, so `audit_log` has no trail of them. Getting the number
  needs a sampling campaign or instrumentation first.
  ⚠️ **And the method has a survivorship problem AB raised against their own
  proposal:** heartbeats ride tool calls, so a distribution of heartbeat
  intervals is a distribution over *sessions that call tools*. Idle sessions
  contribute NO samples — so it would read tighter than reality, and the exact
  population the floor exists to protect is the one missing from the data.
  Collecting more of the same data does not fix that; the floor needs a
  different basis, or the item needs dropping (see AB's own escape hatch: if
  idle gaps are long, a farewell should barely shorten anything and SEAT-13 is
  not worth its risk).
  **GATED** on AgentBeast's **MERGE-2** (two live sessions in one folder
  rendering one picker row — measured 2026-08-01 and confirmed real; AB
  renamed it from their SEAT-14, which collided with an existing pin of
  theirs). Note the ID is AB's, not ours. Until it is fixed, an accelerated
  reclaim would land under a surface still showing one healthy row, and
  neither side could reconstruct afterwards which session lost its address —
  engram's own collision detector is blind to the same case (see SEAT-15).

- **SEAT-15** *(downgraded 2026-08-01 to a PREFERENCE, not a defect — both
  maintainers agreed the underlying problem dissolves rather than gets fixed:
  a store only ever sees what speaks to it, so NO claiming policy makes seat
  rows a session census, and any consumer treating them as one is wrong by
  construction. AgentBeast is fixing their consumer (MERGE-2) by counting
  their own spawns instead of inferring the count from which of them called a
  tool, and is no longer waiting on this.)*
  Decide whether seat allocation should stay LAZY. A session
  claims its seat on its first engram tool call, not at startup:
  `_claim_seat` is reachable only from the heartbeat (which rides tool
  handlers) and `memory_take_seat`, and the bridge has no background beat.
  Verified 2026-08-01. Two consequences worth a decision rather than a
  shrug. (1) Anything reasoning about "the set of seated sessions" is really
  reasoning about "the set of sessions that have called an engram tool" —
  which surprised a peer building on it. (2) Seat-collision detection is
  disarmed for exactly the case that matters here: it needs both sessions in
  the nonce map, a nonce only lands there via the heartbeat, so a second
  session that never calls a tool is structurally invisible to it — and per
  AgentBeast's measurement that is a state a session can occupy for its whole
  life, not a startup window. The coverage limit is now documented at
  `SEAT_COLLISION_WINDOW_SECONDS`. **Both maintainers lean STAY LAZY; owner
  decides, because it touches every box.**
  · *For eager:* it would make "seated" mean "exists", which is what every
    consumer already assumes — AgentBeast built on that assumption, which is
    how their one-row defect happened.
  · *Against eager (AB):* claiming at startup makes seat existence track
    PROCESS existence, re-fusing addressing to presence at the seam this
    project separated on 2026-08-01. Correct the consumers rather than bend
    the model.
  · *Against eager (engram, the load-bearing one):* an address exists to
    participate in messaging. A session that never calls an engram tool is
    not a participant, and eager claiming would allocate an address — and
    burn an ordinal against `MAX_SEAT_ORDINAL` — on process start rather than
    on need, for sessions that will never send or receive anything.
  · ⚠️ *Neither option fixes the detection blindness*, and that is the thing
    to be clear about: collision detection is NONCE-based via
    `presence_update`, so eager SEAT claiming would not populate it. Eager
    would fix a consumer's rendering, not engram's detector.
  ⚠️ **NEW EVIDENCE 2026-08-05, and it is the strongest case for eager yet —
  not from the model, from DISCOVERABILITY.** Which tools trigger a claim is
  undocumented and unguessable: the heartbeat rides five tool handlers, and
  `memory_whoami` is NOT one of them. A peer integrating a new provider drove
  `whoami`, got a correct principal back, and reasonably concluded the session
  was wired up — then spent an hour on four disconfirming probes into the
  addressing layer, because a session with a working tool call and no address
  looks broken in exactly the place the fault is not. **A working tool call is
  not a heartbeating tool call, and nothing in the output distinguishes them.**
  Lazy can stay (the argument above holds), but the discoverability cost is
  now measured rather than theoretical, and at minimum the claim-triggering
  set belongs in the docs and in `memory_whoami`'s own output — an identity
  report that cannot say "you hold no address yet, call any memory tool to
  claim one" is the surface most likely to be asked, answering everything
  except the thing that is wrong.
  **Proposed resolution, consistent with the liveness split:** detecting a
  second session that never speaks to engram is ORCHESTRATION's job, not the
  store's. The orchestrator knows what it spawned; the store can only ever see
  what speaks to it. Engram's duty is to say so plainly rather than to stay
  silent in a way that reads as reassurance — which is now done.

## Owner's drivable menu — store & ops (start when the owner names one)

- **MEM-3** *(supersede verb SHIPPED + fleet-deployed 2026-08-10, `ec6518a`,
  built the day it bit — a departed agent's stale project notes were
  uncorrectable by its successor and out-ranked their own corrections at
  startup-sweep limits. What remains:)* the search-time collapse of same-key
  duplicates across writers. Supersede removes the need for the incident case
  (the stale twin drops out of default search), but two LIVE rows sharing a
  key still both rank with nothing marking which is authoritative — a
  ranking-layer design question, deliberately not smuggled in under the
  incident fix. Resolve/lifecycle for a writer's OWN rows (the original MEM-3
  ask) also still open; supersede covers the cross-writer half only.

- **DATA-1** *(narrowed 2026-08-12: the pre-rewrite history bundle is
  DELETED on the owner's word — its rollback purpose was spent. What
  remains is a different decision than first framed.)* The 2026-07-23
  inbox recovery export (two JSON files, untracked at `~/projects/` top
  level, inside FleetBackup's source set) is the ONLY SURVIVING COPY of
  the 69 messages that outlived the inbox bulk-delete incident — the
  store deliberately does NOT hold them (re-import would wake every
  addressed session with stale mail), and the other ~1664 rows are gone
  permanently. So this is not "delete a redundant archive"; it is "keep
  or erase the last copy of that history." Owner's call, made with that
  fact stated. Pointer: `shared:reference/inbox-recovery-archive-2026-07-23`.

- **MEM-7** Shared lessons are write-mostly — now MEASURED, not argued
  (audit 2026-08-13, `audit/mem-7-lesson-corpus-2026-08-13` in project
  memory). Telemetry already existed: search bumps `last_used_at` on
  returned rows, so exposure is directly readable. 882 lessons: **370 (42%)
  never surfaced by any search since creation; 217 (25%) not surfaced in
  90+ days** — while a ~500-lesson working set IS served regularly. The
  dead weight also dilutes ranking for the working set. The corpus has 0
  superseded rows: the retirement verb exists and has never been used on
  it. Curation plan (batched, never bulk — the tiny-lesson cluster sampled
  heterogeneous: valid patterns, misfiled project content, platform-absorbed
  process rules, dead tech facts): see the audit memory. The owner's index +
  startup-taste build comes AFTER the batches — indexing today's corpus
  would index the noise. Candidate directions, none chosen:
  task-shaped recall (query shared at task boundaries, not just startup),
  consolidation passes (many near-duplicate micro-lessons from the early
  months), usage-weighted ranking, or a curated class-lesson index. Related:
  the same recall economics is why MEM-3's authority-ranking question matters.
  ★ Owner direction (2026-08-13): build an INDEX of sorts and "pump a taste at
  startup" for exposure — otherwise the lessons are a waste of time. Staleness
  is the second question: nobody knows how many of the 878 are now wrong.
  CURATION IS THE LIBRARIAN'S STANDING JOB — periodic passes that consolidate,
  retire stale entries, and make the survivors seen. Owner-named as radar, not
  yet scheduled as a build.

- **ACCEPT-1** Per-provider identity acceptance harness — **engram half
  BUILT and GREEN 2026-08-13** (`acceptance/`, run via `scripts/accept.sh`,
  ~30s: real server on a scratch port + `engram_accept` DB, real bridge
  sessions per provider shape, real watcher; 8 passed / cursor loudly
  skipped). Assertion list v2 ratified same day (drafted engram,
  adversarially reviewed agentbeast-grok, all three objections accepted):
  full list in project memory at `backlog/ACCEPT-1`. The review splits are
  load-bearing — A3 register-vs-surface, A9 crash-vs-after-release, A2/A8
  outcome-vs-mechanism — and the first run vindicated them: the listen_set
  counter-example from the review materialized live (an unseated victim's
  seat IS the project group address, so group-visible mail read as a "leak"
  until the test was made well-posed). REMAINS OPEN for the joint half:
  real-launcher runs per provider (AB spawns, G2 codex key injection) and
  the A3-surface picker check (manual until AB exposes picker state — H3
  work, unnamed).

## Blocked-external

- **DOCKER-1** Verify the full-stack compose path (build, health,
  store/search roundtrip), then promote it from "experimental" in
  README/deployment docs. Blocked: NO healthy Docker runtime exists on the
  fleet (surveyed 2026-07-21 — Linux spokes have no Docker; macmini's
  OrbStack hangs on daemon start). Needs a runtime repair or a fresh box
  first; the stack definition itself is unproven, not suspect.
