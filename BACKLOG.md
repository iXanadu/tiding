# engram — BACKLOG (open items only)

> Open work only. Done = delete the line (its story lives in the commit and in
> memory). No secrets, PII, client names, topology, or exploit detail — this
> file is written as if public. Journal → engram memory. Standard:
> [docs/backlog-standard.md](docs/backlog-standard.md).

## Now (blocking or next up)

- **ID-2** `memory_take_seat` is **silently reverted** on launcher-spawned
  sessions. The tool exists so a session can be re-addressed mid-flight when
  someone decides two agents are co-working in one folder; it sets the
  runtime seat and writes the seat file, which is what carries the change to
  an already-running watcher. But such a session also re-claims every
  heartbeat, and the registry answers from its own record keyed on
  `session_key` — so it returns the seat it already holds, `_claim_seat`
  sees `granted != preferred`, and overwrites the file the agent just set.
  The runtime seat is undone within one heartbeat. The tool reported
  success and handed back a re-arm command; nothing reports the reversal.
  Two mechanisms answering "who is this session", with the loser never told
  — the shape that ran through the whole 2026-07-26 investigation.
  **Verified, not inferred:** pinned by a bridge test that takes a runtime
  seat, runs one claim, and asserts the file was overwritten. Observed live
  first — a probe session took `<proj>-claude-opus5` 71s after a restart
  while the registry held `<proj>-claude`, and the seat-file mtime proved
  the session itself wrote it post-restart. Matters because /startup step
  4a-bis tells co-working sessions to take a seat, so the documented
  co-working path is the one that breaks. Decide whether a runtime seat
  should be registered with the server (so continuity returns it) or
  whether the tool should refuse when a launcher already seated the session.
  **Related hazard, same file:** seat files SURVIVE teardown, and the file
  outranks `ENGRAM_INBOX_IDENTITY`. So a stale file left by a dead session
  silently seats any future session that reuses that `session_key` — at
  whatever the corpse was called — until the first claim overwrites it.
  Confirmed live 2026-07-26: a relaunch resolved a dead session's
  `-opus5` name from the leftover file and was corrected ~7s later by the
  claim. Harmless only because the claim is prompt. Teardown should remove
  the seat file, or the file should carry the nonce it was written for.
  **Unexplained, recorded because neither side could account for it:** the
  same probe recipe launched twice took a runtime seat on the first run and
  not the second, so the live take_seat→revert sequence was never caught in
  the wild despite two attempts. The constructed test carries that half
  alone. Whatever makes the runtime seat conditional is not understood.

- **ID-1** An unconfigured session silently becomes `admin` for ADDRESSING,
  because engram answers "what project is this?" with two resolvers of
  different strictness. Memory operations go through
  `ensure_project_identity`, which RAISES on a genuinely-unconfigured
  directory so the tool layer can interrogate the user — deliberate, per
  SU-1's "interrogate, don't default". The seat path goes through
  `derive_project_name`, which silently falls back to `admin`. So a
  directory engram REFUSES to guess about for memory, it quietly calls
  `admin` for addressing — and since `admin` is seat-exempt (deliberate
  role-sharing), no seat row is created and nothing surfaces. A throwaway
  session in a scratch dir does not merely lack a seat: it adopts the
  administrator's identity, with the exemption suppressing any signal.
  Observed 2026-07-26 when a peer's probe session got no seat and was
  nearly filed as a BRIDGE-1 sighting; it was this instead. Decide whether
  the seat path should adopt the strict resolver, or whether the `admin`
  fallback should be visible rather than silent. Same shape as the rest of
  the day: two paths answering one question differently, permissive one
  winning quietly.
  **Not just scratch dirs — production ones.** Rung 2 is a `/projects/`
  path segment, so everything under `~/projects` is safe without a cfg.
  `/opt/srv` satisfies neither rung 1 nor rung 2, and it is a configured
  launcher root. Audited 2026-07-26 — under `/opt/srv`, these carry a real
  cfg: AgentBeast, FleetBackup, FleetVault, engram. These do NOT and would
  resolve to `admin` if a session were launched in them: `ProjectTracker`,
  `claude-memory-mcp`, `engram-backups`, `ha-semantic-memory`. Latent only —
  no session runs in any of them today. Of the four, three are dead weight
  (two archived repos, plus the stale manual dump dir already proposed for
  deletion), so `ProjectTracker` is the sole live concern. The launcher-side
  half is AgentBeast's (warn when a target would resolve to `admin`,
  exempting `~/maintenance` where shared identity is the point) — and
  writing `.engram.cfg` into another project's tree is NOT the fix.

- **MSG-9** The bridge heartbeat **destroys `watcher_last_seen`**, so the
  BUSIEST session looks the most dead — the exact inversion MSG-5 was built
  to prevent. Two writers, one field, no signal:
  · the watcher beats every 45s via `jsonb_set` (merges, preserves
    everything) — `memory_service.py:890`
  · the bridge heartbeat builds a FRESH metadata dict with a fixed key set
    that does not include `watcher_last_seen`, and writes it WHOLESALE
    (`metadata = $2::jsonb`) — `memory_service.py:789-803`
  So every bridge beat wipes the field and the watcher restores it on its
  next poll. Since the bridge beat rides TOOL CALLS, an active session
  overwrites far more often than the watcher restores, and its
  `watcher_alive` reads null essentially forever. An idle session keeps the
  field and looks healthy.
  **Consequence:** `watcher_alive` — the positive death signal introduced by
  MSG-5, and the one a peer's roster consumes — inverts under load. A
  session hammering tool calls (i.e. demonstrably alive) advertises itself
  as NOT LISTENING. Reproduced 2026-07-27 on this very session: three peers
  showed fresh watcher beats, `presence/engram-claude` showed none, while it
  was provably waking on mail throughout.
  **Fix is small:** carry `watcher_last_seen` forward from `prior_md` — the
  same row is already read for the `sessions` nonce map a few lines above.
  Add a test that a bridge beat following a watcher beat preserves it.
  Same family as SEAT-12 and MERGE-1: a field with two writers where one
  silently clobbers, and nothing reports the loss.

- **SEAT-12** The seat-collision detector **false-positives on every
  restart**, and the fix now exists. `_fresh_sessions` keeps any nonce seen
  within `SEAT_COLLISION_WINDOW_SECONDS` (300), so a restarted session's
  dead predecessor counts as a second live session for five minutes and the
  identity is flagged as colliding. The code comment concedes it — "a bridge
  restart mid-session can look like a collision for at most this long" — and
  it was an accepted cost because nothing could distinguish a corpse from a
  rival. **SEAT-9 changed that:** the seat row now records
  `superseded_nonces`, and a displaced nonce is by definition not a rival.
  The detector simply never consults the field. Excluding superseded nonces
  from the fresh set retires the false positive outright.
  Reproduced live 2026-07-27: a probe reported "2 live sessions" for an
  identity that process truth showed had exactly ONE bridge. It was raised by
  the probe itself as a caveat against its own passing result — correct
  instinct, wrong facts — and only a peer's process-tree check kept two
  experiments interpretable. Worked example of why the two liveness sources
  (registry claim vs process ancestry) are both needed: the registry was the
  one that was wrong.

- **MSG-7** Mail that arrives while a session is RESTARTING is **silently
  absorbed as history**, not delivered as a directive. The next session's
  `/startup` sweep reads the inbox and drains it — the queued message is
  acked/resolved and becomes prior context, so it never surfaces to the
  running agent as "you have mail" and is never acted on. Observed
  2026-07-27 on a restarted probe whose pane read `Inbox: empty (3
  previously-open, 1 resolved, all hidden/closed)`, found by a peer who
  sent into a restart window by accident and then checked the pane instead
  of assuming the send was harmless.
  **Why it is not merely "a weaker result":** queued-while-down and
  delivered-to-a-live-session are DIFFERENT OUTCOMES, not degrees of one.
  Any manager/driver pattern that restarts a worker and then instructs it
  hits this — the instruction is read as history instead of obeyed, with
  no error on either side. The sender sees a successful send; the worker
  sees context. Compounding: `intent=action` currently survives into the
  startup read but carries no "this was addressed to your predecessor"
  marker, so the reader cannot tell a live directive from drained backlog.
  Decide whether startup should leave `intent=action` mail UNACKED (so it
  still wakes the new process), or mark restart-window mail distinctly.

- **BRIDGE-1** (hardening, not urgent) The bridge's seat-claim path fails
  **silently and permanently**: one unresolvable session key latches a
  per-session flag that is never cleared, and every other failure is
  swallowed by a bare `except: pass`. A session could go its whole life
  never claiming and emit nothing, and the server cannot tell "never
  claimed" from "not running" — both are an absence. Best-effort was the
  right call for AVAILABILITY (a session must not fail to start because the
  address service is down), but silent and permanent are separable from
  best-effort; conflating them is the defect. Needs a signal in tool
  guidance and a retrying latch. Bridge change — lands at each session's
  next start.
  **Not observed firing.** Raised 2026-07-26 as the explanation for a peer
  whose seat looked stuck; that turned out to be the peer's own
  arbitrary-pick bug manufacturing the symptom, and their bridge had been
  claiming correctly throughout. The shape is still worth fixing on its own
  merits, but nothing has yet been traced to it. Named BRIDGE-1 rather than
  SEAT-N deliberately: it is a bridge item, and the SEAT-N space is shared
  with AgentBeast's ledger where the numbers already mean other things.

- **SEAT-6** Grok seat integration is incomplete (AgentBeast-owned, engram
  needs no change). Grok already carries `ENGRAM_PROVIDER` — the roster and
  AB's enumeration both report `provider=grok` correctly (AB fixed the
  enumeration in `b82860a`). What remains: grok gets no `ENGRAM_SESSION_KEY`
  on AB's launch *start* path, so a grok seat has no stable key across a
  respawn (grok effort/model changes stop→respawn), meaning it can't reliably
  re-claim the same seat the way Claude does. Not an engram instability and no
  regression — grok works today on its launch-injected identity.

- **SEAT-4** Roster lifecycle (registry Phase 2). Presence rows are never
  released: the live roster carries entries days stale, all still reporting
  `state: running`, because self-reported state is never corrected once a
  session dies (observed: a session reporting `running` four hours after it
  died). Mark entries past grace `presumed-dead` and let the cleanup task
  drop rows past a retention horizon. **Narrowed by MSG-5:** a dead session's
  watcher dies with it, so `watcher_alive` flipping to `false` is now the
  positive death signal `is_stale` never had — what remains is acting on it
  (correcting the stale `running`, and a retention horizon for the rows).
  **THE FIRST ATTEMPT SHIPPED AND WAS FALSIFIED THE SAME DAY (`3ced53e`,
  13:00; power outage 17:24Z).** It corrected a corpse's `running` when
  `watcher_alive=false`, on the premise that "a watcher that HAS beaten and
  then stopped is a process that exited." **That premise is false across a
  RESTART BOUNDARY.** `watcher_last_seen` survives on a presence row that the
  next generation reclaims through SEAT-9 continuity, so generation N's
  watcher evidence gets applied to generation N+1's state. Verified in the DB
  seven minutes after boot, not inferred:
  · `watcher_last_seen` 17:23:53Z — 37s BEFORE the power died, dead generation
  · `last_seen` 17:28:34Z — post-boot, live generation
  · roster rendered `presumed-dead`; the process was alive and running
  The session was genuinely DEAF (its watcher had not been re-armed yet) but
  not dead — and the render self-contradicted, state saying `presumed-dead`
  while the banner below said `running`. **Control case proving the rule is
  not simply broken:** a peer with a 15-hour-old presence beat was correctly
  marked. The inversion fires ONLY when the presence beat is FRESHER than the
  watcher beat — i.e. exactly the post-restart window, which is the normal
  path (the watcher arms a minute or two into `/startup`), and which a power
  outage triggers fleet-wide at once.
  **Window measured: ~5 minutes, and it closed by itself.** Session up
  17:28:18Z, its watcher armed 17:33:16Z (process start time, authoritative)
  — the entry then read `running 👂 listening` again. The self-heal is real
  and needs no intervention: `/startup` step 4b arms the watcher a few
  minutes in, and the first beat corrects the record.
  **Structural residual, NOT demonstrated here:** nothing *causes* that beat
  except a session reaching 4b, so a session that never arms a watcher would
  stay falsely dead for its whole life. That remains an inference — every
  session observed on this box did arm one.
  *(An earlier revision of this entry claimed ~41 minutes and "ends only when
  a human intervenes." Both were wrong: the 18:09:35Z figure was the latest
  beat read off the row, not the first, and the row cannot show a first beat.
  The error is the same one the entry is about — stale evidence asserted as
  current state — and it was caught by a peer's pushback, not by review.)* **Third instance of one shape** (MSG-9,
  SEAT-12, this): two writers on one row, no generational guard, loser never
  told — and again the registry was wrong where process-ancestry was right.
  The discriminator is already in the row (a watcher beat older than the
  holder's own presence beat cannot speak for that holder), but the guard we
  actually trust is the NONCE, not a clock — SEAT-9 already records
  `superseded_nonces`. **The inversion is now FIXED** (nonce guard): the
  presence beat carries `watcher_last_seen` forward only WITHIN a generation,
  and a nonce absent from the prior map clears it — yielding `None` (no
  basis), never `False` (dead), so the three-valued discipline holds and the
  new generation's own watcher restores truth on its next poll. A genuine
  corpse receives no beats at all, so it still ages to `presumed-dead`
  exactly as intended. AgentBeast confirmed nothing had changed on seat
  ownership and endorsed the nonce over a clock: a guard whose correctness
  depends on time being sane is worthless across a boot, which is precisely
  when it fires.
  **REDIRECTED 2026-07-27 — the fix is a TOMBSTONE ON EXIT, not better
  polling.** *(And an outage is precisely the case where no tombstone is ever
  written — so the falsification above is also evidence about how much weight
  the tombstone design can carry on its own.)* A phantom seat was measured lasting ~4.5 minutes against a
  known-good count of one live process: the dead generation's row simply
  aged out on its own TTL because a graceful exit never invalidated it.
  Polling cannot beat a stale row nobody retracted. And per MSG-8 it must
  not try: a busy agent in a long tool call is silent exactly like a dead
  one, so any TTL-based liveness will age out a session that is working
  normally. Those are the same defect in two hats — the answer is for a
  departing session to say so, not for observers to guess harder.

- **MSG-8** **A message cannot interrupt an in-flight tool call — and the
  dominant latency is the WATCHER POLL, not the block.** Two terms, and we
  first attributed all of one to the other.
  **MECHANISM (measured, stands):** a session has no execution context while
  a tool call is in flight. A probe head-down in one blocking 90s call
  produced ZERO mid-loop markers; detection came only after the call
  returned. Chunking the same 90s of work into ten short calls DID produce
  mid-run detection, twice. Same work, same watcher — only the boundaries
  differed.
  **QUANTITY (corrected 2026-07-27, do not resurrect the old figure):** the
  first run was reported as "89s deaf, caused by the blocking call". That
  attribution is WRONG. Decomposed: send 02:13:57.2Z, blocking call returned
  02:14:23Z, first observable 02:15:26Z — so the block accounts for at most
  25.8s (29%), and 63.0s elapsed while the agent was NOT blocked. That
  remainder is poll latency. `engram-inbox-wait --poll-interval` defaults to
  **45s** (inbox_wait.py:256); no watcher on the fleet overrides it.
  Corroborated independently: two detections in the chunked run were 46.0s
  apart — one poll cycle.
  **So: the boundary is a GATE, the poll is the CLOCK.** Chunking removes
  the hard block; it cannot buy responsiveness below the poll interval. A
  worker chunked to 1s calls is still up to 45s stale.
  **Not an engram defect in the blocking half** — delivery, addressing and
  the watcher all worked. The poll half IS tunable and ours.
  **Caught only because a single observation cannot separate two additive
  latency terms.** The second run supplied the second observation. Any
  future latency claim here needs two measurements at different chunk
  shapes, or it is attributing a sum to whichever term was being discussed.
  **THE CONSEQUENCE, which is the load-bearing part:** a BUSY agent and a
  DEAD agent are indistinguishable from outside — both are silence. So you
  cannot interrupt a working agent, AND any liveness check that reads
  silence as death will kill working agents mid-task. Any manager/driver
  design must account for both directions.
  **THE MITIGATION IS TWO PARTS, and only one was in the original model:**
  (1) CHUNK the work — removes the hard block, restores mid-job
  reachability; (2) LOWER `--poll-interval` — sets how stale a worker can
  be. Chunking alone gives mid-job detection with up-to-45s lag. Whether
  45s is acceptable for steering a worker is the owner's call, and it is
  now a tunable rather than an unknown.
  **STILL OPEN:** confirm the poll interval is the WHOLE remaining term —
  same chunked shape with `--poll-interval 5`, expecting first-boundary
  detection. Cheap. Not run.
  **TEST-DESIGN DEFECT worth keeping:** the agreed discriminator "the marker
  must land at a CHUNK BOUNDARY" was DEGENERATE — an agent can only execute
  at boundaries, so a marker can land nowhere else, and it was guaranteed to
  pass by construction. The version with content is "the FIRST boundary
  after t_send", and by that the chunked run FAILED both times (2 boundaries
  late, then 4). Two of us reviewed that discriminator and neither noticed;
  the probe that built the test caught it afterwards.
  Method notes worth keeping for any rerun: the naive "message a busy agent
  and see if it answers" is UNFALSIFIABLE (the launcher does not log prompt
  sends, so an autonomous wake and a prompted turn leave identical traces —
  a peer's ledger carried this as PROVEN for a day on that inference; see
  `shared:lesson/a-proven-backlog-entry-is-the-most-dangerous-object-in-a-repo`).
  Put the discriminator in the WORKER'S OWN ORDERED OUTPUT, stamp wall-clock
  beside each step, launch the probe with `remote_control: false`, time off
  the last OBSERVED watchdog pass rather than a projection, and write down
  what a negative looks like before running.

- **MSG-10** **Mail carries no origin-machine provenance.** Every client
  stamps `X-Engram-Machine` on every request — the header is set in
  `MemoryClient.__init__` from the hostname — and the send path never reads
  it. `x-engram-machine` is consumed in `set_memory` only, so `/memory/set`
  rows get `metadata.machine` and inbox rows do not. The one field that
  could answer "which box did this come from" is on the wire and discarded
  at the last step, which is the same shape as the inbox render dropping
  `created_at` and the search count that was a page size.
  Found 2026-07-27 while closing MSG-6: a message sent from WebOne through
  its real bridge client stored `machine: (none)`. Not urgent — mail is
  addressed and delivered correctly without it — but it means a
  five-box fleet cannot attribute a message to a box after the fact, and it
  is the natural discriminator for any future cross-machine question.
  Cheap: one parameter through `inbox_send`, one header read at the router.

- **DR-3** Consider enabling WAL archiving. The backup chain itself is
  sound and was drilled end-to-end 2026-07-25: dumps every 30 min (114 runs,
  zero failures), captured onsite and offsite, and a dump already rotated off
  local disk was recovered from the archive and restored cleanly. So the gap
  is not coverage — it is GRANULARITY. `archive_mode=off` means no
  point-in-time recovery between dumps, and the floor is therefore ~30
  minutes. Demonstrated the same evening: a sub-second overwrite destroyed a
  peer's content that no snapshot ever contained, because the whole race
  resolved inside one backup window. No backup cadence fixes that class —
  only a correct guard does. Decide whether the operational cost of WAL
  archiving is worth closing the remaining window.

- **OBS-1** The application log carries **no timestamps** — 840k+ lines of
  uvicorn access output, not one of them dated. Found 2026-07-25 while
  investigating a reported data loss: the log proved that `/memory/forget`
  was called, and could not say *when*, so it could not be tied to the
  incident or to any session. Combined with AUDIT-1 (no write trail) and
  Postgres `logging_collector=off`, there is currently **no dated record of
  any destructive operation anywhere in the stack**. Configure the uvicorn
  access formatter with a timestamp; this is a config line, and it is
  cheaper than any of the forensics it would have replaced.

- **AUDIT-1** `audit_log` has **zero rows** — the table ships with the
  principals work and nothing ever writes to it. So there is no write trail:
  the store cannot answer "who wrote what, when." Proven costly on
  2026-07-24, when "did a shut-down agent store its findings?" needed three
  inferential DB queries by hand instead of one lookup against an append-only
  log, and still could not fully rule out an overwrite. Compounding factors:
  there is no `updated_at` column, and `last_used_at` bumps on **reads**, so
  it is useless as a write signal. Decide what to record (writes at minimum:
  principal, key, scope, project, timestamp) and its retention.

## Needs decision

- **SEC-7** Decide whether `/memory/set` should reject unknown fields
  (`extra="forbid"`, as `/admin/bulk-delete` already does). **This is
  ergonomics, not a safety gap** — an earlier version of this line overstated
  it. A misspelled guard field (`if_matched`) is ignored, so the write is
  unconditional and `if_match_applied` correctly reports `false`; a caller
  checking that signal fails closed and declines to merge (pinned by test).
  So the typo degrades to "merges never happen, loudly" rather than
  "unguarded write reported as safe." What `extra="forbid"` would add is
  turning a mysterious never-merges into an immediate 422 at the call site —
  worth real debugging time, not a correctness hole. **Against:** engram is
  public and this is a breaking change; any adopter's client sending a stray
  field starts getting 422 on upgrade.

- **MEM-2** Key-prefix enumeration — a deterministic "list every key under
  `wip/`" that returns ALL matches in key order with no embedding involved.
  `memory_get` is exact-match, `memory_search` is semantic, and there is
  nothing in between; measured live, handoff notes score 0.51 while an
  unrelated five-month-old note scores 0.45, so "read all the open handoffs"
  is not reliably expressible and a missing one is indistinguishable from
  none. Would honor namespace read perms exactly as search does. Requested by
  AgentBeast 2026-07-24. **Justified today, independent of any multi-session
  design** — sibling `wip/*` keys have sat unread for weeks in a
  single-session world because `/startup` fetches exact keys and everything
  else is invisible by construction. What the owner's handoff direction gates
  is this item's PRIORITY, not its validity.
  **Live incident the same evening:** an agent was shut down mid-job and the
  question "did it store anything?" could not be answered from any client —
  semantic search cannot establish absence, and eight differently-phrased
  searches returning nothing is evidence, not proof. Answering it took direct
  SQL. The real form of this capability is not a multi-session nicety; it is
  "did an agent's work survive its own shutdown."

- **MEM-3** A lifecycle verb for memories — resolve/supersede, copying the
  inbox's existing pattern rather than inventing one (same table, same
  metadata status field; a row with no status reads as live, so it is
  back-compatible). Today memories have only create and delete, so the sole
  way to stop stale handoffs accumulating is `memory_forget`, which destroys
  the history. "Delete is the only lifecycle verb" is a shape worth removing
  after the 2026-07-23 incident. Same gate as MEM-2.

## Next (committed, not started)

- **DATA-1** Decide the fate of two sensitive local archives sitting
  untracked at `~/projects/` top level (deliberately outside every git
  repo, inside FleetBackup's source set): the 2026-07-23 inbox recovery
  export, and the older pre-rewrite history bundle. Both contain verbatim
  private conversation and neither is managed by any retention policy —
  they persist until someone decides. Owner's call: keep, relocate to a
  vault, or delete. Pointer: `shared:reference/inbox-recovery-archive-2026-07-23`.

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

## Blocked-external

- **DOCKER-1** Verify the full-stack compose path (build, health,
  store/search roundtrip), then promote it from "experimental" in
  README/deployment docs. Blocked: NO healthy Docker runtime exists on the
  fleet (surveyed 2026-07-21 — Linux spokes have no Docker; macmini's
  OrbStack hangs on daemon start). Needs a runtime repair or a fresh box
  first; the stack definition itself is unproven, not suspect.

## Later / decide

