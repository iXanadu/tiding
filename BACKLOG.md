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

- **MAIL-1** Rob's ruling 2026-07-27 (huddle kgKq6dH9), pinned on his "take
  note — no fix now": the owner-facing "Mail" surface must become either
  (a) REMOVED, or (b) "messages DIRECT to me that I have not seen" — huddle
  traffic belongs in the huddle, not listed in two places. Engram's half when
  scheduled: an OPT-IN `direct_only` filter on `/memory/inbox` (exclude
  `huddle/`-threaded rows; unread-only already exists via read-state). The
  DEFAULT view must NOT change — agent watchers wake on huddle rows through
  it, and changing the default would silently deafen the fleet. Surface half
  (wiring Mail endpoints/tabs to the filter, or removing the tab) is
  AB/app's.

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
  as its own clock. **(b) CHOSEN, and its open question is closed.**
  AgentBeast's condition was that revocation needs TIME to work: instant
  eligibility would leave a false farewell no window in which to be revoked,
  quietly removing the safety net. Checked rather than assumed —
  `_presence_is_fresh` means "no presence within `SEAT_LIVE_SECONDS`", so the
  existing guards already supply a **10-minute** quiet period, ~13 watcher
  polls, and revocation fires on the session's own heartbeat or a re-armed
  watcher, whichever speaks first. The window was FOUND, not chosen; no new
  number enters the design. Remaining work is purely the implementation.

## Owner's drivable menu — store & ops (start when the owner names one)

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
  after the 2026-07-23 incident. Natural pair with MEM-2 (same part of the
  codebase; together they make memory provable and curatable).

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

- **DATA-1** Decide the fate of two sensitive local archives sitting
  untracked at `~/projects/` top level (deliberately outside every git
  repo, inside FleetBackup's source set): the 2026-07-23 inbox recovery
  export, and the older pre-rewrite history bundle. Both contain verbatim
  private conversation and neither is managed by any retention policy —
  they persist until someone decides. Owner's call: keep, relocate to a
  vault, or delete. Pointer: `shared:reference/inbox-recovery-archive-2026-07-23`.

## Blocked-external

- **DOCKER-1** Verify the full-stack compose path (build, health,
  store/search roundtrip), then promote it from "experimental" in
  README/deployment docs. Blocked: NO healthy Docker runtime exists on the
  fleet (surveyed 2026-07-21 — Linux spokes have no Docker; macmini's
  OrbStack hangs on daemon start). Needs a runtime repair or a fresh box
  first; the stack definition itself is unproven, not suspect.
