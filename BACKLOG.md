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

- **MSG-6** Three properties of the transport are UNTESTED — not lightly
  tested, never exercised — and all are engram's rail rather than a client's:
  (a) **mid-job interrupt wake.** Every wake proven to date is of a session
  dormant BETWEEN turns. Whether inbound mail reaches a session stalled
  mid-task has never been tried, and any "keep a worker moving" pattern rests
  entirely on it. (b) **cross-machine delivery.** Clients on other boxes point
  at this server; nothing has been verified across that boundary.
  (c) **huddle delivery across a participant restart.** SEAT-9 proved a
  restarted session keeps its ADDRESS; it did not prove a huddle formed
  BEFORE the restart still delivers to that participant AFTER it. Different
  mechanisms — the seat versus the participant list fixed at send time.
  Cheap and interpretable today.
  Until they run, all three are assumptions wearing the costume of facts.
  **(a) has a designed, agreed protocol — do not run the naive version.**
  "Message a busy agent and see if it answers" is UNFALSIFIABLE here: the
  launcher does not log prompt sends, so an autonomous wake and a prompted
  turn leave identical traces. (A peer's ledger carried this as PROVEN for a
  day on exactly that inference, retracted 2026-07-26 —
  `shared:lesson/a-proven-backlog-entry-is-the-most-dangerous-object-in-a-repo`.)
  Instead put the discriminator in the WORKER'S OWN ORDERED OUTPUT: give it a
  strictly sequential task with one observable step per second, instruct it to
  append a marker the instant mail arrives, and read the marker's POSITION.
  Position inside a sequence is positive evidence; a missing log line never
  is. Confound to instrument rather than dodge: the launcher's watchdog
  perturbs every pane every 300s, so compare the marker's position against
  BOTH the send time and the watchdog cadence — matching the send proves the
  message woke it, matching the cadence catches the confound. Probe must be
  launched with remote-control disabled (`remote_control: false` on the
  launcher API) — but note that only removes the injected-keystroke path;
  the pane is still captured and repainted every pass, which is why the
  control is still needed. Have the worker stamp WALL-CLOCK time beside each
  step, so position-in-sequence and elapsed time cross-check each other and a
  stalled or drifting task shows up instead of being silently reinterpreted.
  Time off the last OBSERVED pass, never a projected one. **Write down what a
  negative looks like before running:** no marker before the end means
  mid-job delivery does not work — a finding, not a disappointment, and it
  gets reported as loudly as a pass.

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

