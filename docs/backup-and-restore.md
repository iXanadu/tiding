# Backup and restore

engram is fleet infrastructure: every agent's durable memory lives in one
Postgres database on one box. This page is how that database is protected and,
more importantly, how you get it back.

> **Why this page exists.** On 2026-07-23 a single bad admin call deleted 1733
> rows, and there was no restore path of any kind — no dump, `archive_mode=off`,
> no PITR, no snapshot. The knowledge tier survived only because the delete
> predicate happened not to match it. A backup hook had been *designed* six
> weeks earlier and never built. The lesson is in the shape of the tooling
> below: **a backup that has never been restored is not a backup, it's a file.**

## The two halves

| Owner | Responsibility |
|---|---|
| **engram** | Produce a correct, restorable, *verified* dump. `scripts/backup-db.sh`, `scripts/restore-db.sh`. |
| **FleetBackup** | Invoke it, get the dump offsite, retain it, and **alert when a backup doesn't happen**. |

Neither half is useful alone. A verified dump sitting on the same disk as the
database it came from protects against operator error (the realistic case) but
not against losing the box.

## Taking a backup

```bash
scripts/backup-db.sh                  # → /opt/srv/engram-backups/engram-<ts>.dump
scripts/backup-db.sh --out-dir DIR --keep 14
```

- `pg_dump --format=custom`, so `pg_restore` can extract **individual tables**.
  The realistic disaster is "one table got wiped", not "the disk died", and
  plain SQL would force an all-or-nothing restore over a live database.
- **Verified at creation, not at restore.** The script reads the archive's
  table of contents and asserts `memories` table data is present. A truncated
  or empty dump is deleted rather than kept — a well-formed file that would
  have saved nobody is worse than an obvious failure.
- **Fails loud.** Exit 0 means a usable dump exists and its path is the last
  line of stdout. Any non-zero exit means *no usable dump was produced*.
- Finds `pg_dump` itself rather than trusting `PATH`, so it works under
  launchd/cron where homebrew is not on the path.
- Keeps `--keep` generations locally so a bad dump can never overwrite the last
  good one. Offsite retention is FleetBackup's concern.

## Restoring

The default mode is the safe one. **You cannot reach production by forgetting a
flag** — that property is deliberate, because the incident that made this
tooling necessary was a destructive operation that was easier to run than to
verify.

```bash
# Prove the newest backup actually works. Restores to a scratch DB,
# reports row counts, drops the scratch DB. Run this regularly.
scripts/restore-db.sh --rehearse

# Restore to a named scratch DB and KEEP it — for pulling specific rows
# back by hand after a partial loss. This is usually what you want.
scripts/restore-db.sh --file <dump> --into engram_forensic

# Overwrite the live database. Requires --i-understand AND a typed
# confirmation of the database name.
scripts/restore-db.sh --file <dump> --production --i-understand
```

**Recovering a subset** (the common real case — one table or key-prefix wiped)
is `--into` a scratch DB, then copy the rows you need across. Do not restore
over production to recover a subset; you would trade a partial loss for the
loss of everything written since the dump.

Before `--production`: stop the service (`scripts/stop.sh`), or live writes
race the restore.

## Verifying it works

```
$ scripts/restore-db.sh --rehearse

  RESTORED CONTENTS of engram_restore_rehearsal_84716
    memories (total) : 3099
    ├─ inbox rows    : 14
    └─ knowledge rows: 3085
    principals       : 7
    live DB for comparison: 3099 memories

restore-db: REHEARSAL PASSED
```

The rehearsal **fails** if the restored database has no memories. A green exit
code from a backup job is not evidence; matching row counts are.

## What is NOT protected

Be honest about the edges rather than implying total coverage:

- **Point-in-time recovery.** `archive_mode=off`, so recovery granularity is
  "the last dump". Anything written between the last dump and the incident is
  gone. Enabling WAL archiving would close this and is not done.
- **The window.** A dump taken every N hours means up to N hours of exposure.
- **Same-disk-only local dumps.** Until FleetBackup picks them up, the dumps
  sit on the same machine as the database. That covers operator error, not
  hardware loss.
- **Other boxes.** This protects the macmini database, which is the only one
  that holds fleet memory.

## Related

- `shared:lesson/destructive-endpoint-assumed-safety-flag` — the incident.
- `shared:reference/inbox-recovery-archive-2026-07-23` — what was recovered
  from it, and from where.
