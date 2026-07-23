# engram — BACKLOG (open items only)

> Open work only. Done = delete the line (its story lives in the commit and in
> memory). No secrets, PII, client names, topology, or exploit detail — this
> file is written as if public. Journal → engram memory. Standard:
> [docs/backlog-standard.md](docs/backlog-standard.md).

## Now (blocking or next up)

- **DR-2** Get the verified DB dump OFFSITE. `scripts/backup-db.sh` +
  `restore-db.sh` exist and the restore is rehearsal-proven, but dumps
  currently land in `/opt/srv/engram-backups` — the same disk as the
  database, and NOT inside FleetBackup's `FB_SOURCES` (`$HOME/projects`).
  That covers operator error, not loss of the box. Needs the FleetBackup
  side: pre-backup hook invocation (preferred over a watched directory, so
  the dump is fresh relative to the snapshot), a non-zero hook exit failing
  the backup VISIBLY, retention, and alerting when a backup doesn't happen.
  Coordinating with fleetbackup-claude in huddle Oyf_5Ijf. Until this
  lands, the fleet's memory has local-only protection.

- **SEC-6** `/admin/bulk-delete` silently ignores unknown request fields.
  A caller's assumed safety flag (`confirm:false`) was accepted and the
  delete ran for real, returning the deleted count where a preview was
  expected — this destroyed 1733 rows on 2026-07-23. Fix: `extra="forbid"`
  on destructive request models so an unknown field 422s; add a real
  dry-run that reports matches WITHOUT deleting; require an explicit
  acknowledgement when `key_prefix` is broad enough to match a whole scope.
  An endpoint that accepts an unknown safety flag is worse than one with
  none, because it rewards the caller's assumption.

- **DR-3** Consider enabling WAL archiving. Recovery granularity today is
  "the last dump" — `archive_mode=off`, so there is no point-in-time
  recovery and anything written since the last dump is unrecoverable.
  Decide whether the operational cost is worth closing that window.

## Next (committed, not started)

- **SEAT-2** Make the runtime-reseat split state impossible instead of
  documented. Today `memory_take_seat` moves the bridge instantly while
  the watcher keeps polling under its launch identity — the session is
  addressed at the new seat but listening at the old one. Project mail
  still arrives, so it fails quietly. Fix: watcher re-resolves its seat
  each poll from a per-session file keyed on `ENGRAM_SESSION_KEY` (to be
  emitted at spawn by the launcher; derived from the session handle so it
  survives a respawn, never the pid). Falls back to today's start-time env
  resolution when the key is absent, so hand-launched sessions do not
  regress. The watcher must treat the file as advisory — never crash or go
  silent on a missing/malformed one, since a stale seat still catches
  project-addressed mail. Blocked on the launcher shipping the key.

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

