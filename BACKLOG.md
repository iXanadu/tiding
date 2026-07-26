# engram — BACKLOG (open items only)

> Open work only. Done = delete the line (its story lives in the commit and in
> memory). No secrets, PII, client names, topology, or exploit detail — this
> file is written as if public. Journal → engram memory. Standard:
> [docs/backlog-standard.md](docs/backlog-standard.md).

## Now (blocking or next up)

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

- **MSG-6** Two properties of the transport are UNTESTED — not lightly
  tested, never exercised — and both are engram's rail rather than a client's:
  (a) **mid-job interrupt wake.** Every wake proven to date is of a session
  dormant BETWEEN turns. Whether inbound mail reaches a session stalled
  mid-task has never been tried, and any "keep a worker moving" pattern rests
  entirely on it. (b) **cross-machine delivery.** Clients on other boxes point
  at this server; nothing has been verified across that boundary. Each is a
  single deliberate test. Until they run, both are assumptions wearing the
  costume of facts.

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

