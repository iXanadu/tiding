# engram — BACKLOG (open items only)

> Open work only. Done = delete the line (its story lives in the commit and in
> memory). No secrets, PII, client names, topology, or exploit detail — this
> file is written as if public. Journal → engram memory. Standard:
> [docs/backlog-standard.md](docs/backlog-standard.md).

## Now (blocking or next up)

- **SEAT-3** Deploy the session registry (built, tested, committed, **not
  deployed**). Sessions now claim a server-allocated address instead of
  computing one, so N sessions in a project get N distinct addresses with no
  human step. Deploy needs a `/opt/srv/engram` pull + `com.engram` kickstart;
  each session's bridge picks it up at its next start. Held back
  deliberately: blast radius is fleet-wide addressing and it was built while
  the owner was travelling and unable to observe fallout. Nothing is broken
  today without it. Design + verification:
  [docs/design/session-registry.md](docs/design/session-registry.md).

- **SEAT-4** Roster lifecycle (registry Phase 2). Presence rows are never
  released: the live roster carries entries days stale, all still reporting
  `state: running`, because self-reported state is never corrected once a
  session dies. Mark entries past grace `presumed-dead` and let the cleanup
  task drop rows past a retention horizon.

- **DR-3** Consider enabling WAL archiving. Recovery granularity today is
  "the last dump" — `archive_mode=off`, so there is no point-in-time
  recovery and anything written since the last dump is unrecoverable.
  Decide whether the operational cost is worth closing that window.

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

