# DR-3: Point-in-time recovery for the engram store

Drilled end-to-end 2026-08-11 (base + WAL replay into a scratch instance
recovered all rows including a canary written minutes earlier). Born from the
2026-08-10 power cut, where the loss window was ~30 minutes (dump cadence) and
losing ~nothing was a six-minute fluke of backup timing.

## The chain

| Layer | What | Where |
|---|---|---|
| WAL archives | every segment, gzipped, ≤5 min behind (`archive_timeout=300`) | FleetBackup dump dir, `wal/` |
| Base backups | weekly `pg_basebackup -Ft -z -X stream`, newest 2 kept | FleetBackup dump dir, `base/base-<stamp>/` |
| Logical dumps | every 30 min (pre-existing, independent restore path) | FleetBackup dump dir |

All three live inside the FleetBackup source set, so they ship onsite+offsite
with no additional transport. WAL older than the oldest kept base is pruned by
`pg_archivecleanup` on each successful base backup.

**Loss window: ≤5 minutes** (the current, not-yet-archived segment). Tighten by
lowering `archive_timeout` if ever needed.

## Pieces

- `scripts/wal-archive.sh` — `archive_command` target. Exit-0 only on durable
  archive; never overwrites a completed archive; atomic finalize. A failing
  archiver makes postgres retry forever while `pg_wal` grows — surfaced by the
  doctor's `archiver_ok` check (pages).
- `scripts/pg-basebackup.sh` — weekly base + retention + WAL pruning (cutoff
  read from the oldest kept base's own `backup_label`). Safe to run by hand.
- `launchd/com.engram.pg-basebackup.plist` — Sundays 03:30, installed at
  `/Library/LaunchDaemons/`. The doctor's `basebackup_fresh` check (<8d) pages
  if the schedule silently rots.
- `postgresql.conf` — appended block `engram DR-3` (archive_mode, command,
  timeout).

## Restore procedure (drilled — copy/paste shape)

```bash
BASE=<dump-dir>/base/base-<newest>   WAL=<dump-dir>/wal   TARGET=<new-datadir>

mkdir -p $TARGET && tar -xzf $BASE/base.tar.gz -C $TARGET
mkdir -p $TARGET/pg_wal && tar -xzf $BASE/pg_wal.tar.gz -C $TARGET/pg_wal
chmod 700 $TARGET
cat >> $TARGET/postgresql.conf <<EOF
archive_mode = off                     # do NOT let the restored copy archive
restore_command = 'gunzip -c $WAL/%f.gz > "%p"'
# for point-in-time (instead of end-of-WAL): recovery_target_time = '...'
EOF
touch $TARGET/recovery.signal
pg_ctl -D $TARGET start    # replays archives, then promotes
```

Traps met during the drill, so nobody re-derives them:

1. **`archive_mode = off` in the restored copy is mandatory** — the base image
   carries the live conf, and a promoted copy would otherwise gzip its own
   timeline-2 WAL into the real archive dir.
2. **A canary written and switched in ONE psql `-c` call is not in the switched
   segment** — multi-statement `-c` runs as a single transaction, so
   `pg_switch_wal()` executes before the insert's commit record. Switch in its
   own statement when probing archive coverage.
3. macOS unix-socket paths cap at 103 bytes — deep scratch dirs need
   `unix_socket_directories` pointed somewhere short.
4. `head -n -K` is a GNU-ism BSD head rejects (retention pruning) — caught on
   first live run.
