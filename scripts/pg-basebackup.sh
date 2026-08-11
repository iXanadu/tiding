#!/bin/bash
# pg-basebackup.sh — weekly physical base backup for engram's DR-3 (PITR).
#
# WAL archives alone cannot restore anything: replay needs a physical base
# image to start from (the existing pg_dump chain is logical and cannot
# consume WAL — it stays as the independent second restore path). This
# script produces that base, keeps the newest KEEP of them, and prunes WAL
# archives older than the oldest KEPT base (pg_archivecleanup), so the
# archive dir is always exactly "what a restore could need" and never grows
# without bound.
#
# Runs weekly via /Library/LaunchDaemons/com.engram.pg-basebackup.plist
# (template in launchd/), as the postgres superuser account. Output lands in
# the FleetBackup dump dir so it ships onsite+offsite with everything else.
# Safe to run by hand at any time; concurrent runs are excluded by a lock.

set -u

PG_BIN="${PG_BIN:-/opt/homebrew/opt/postgresql@17/bin}"
DUMP_DIR="${DUMP_DIR:-/Users/ixanadu/.local/state/fleetbackup/dumps}"
BASE_DIR="$DUMP_DIR/base"
WAL_DIR="$DUMP_DIR/wal"
KEEP="${KEEP:-2}"
STAMP=$(date +%Y%m%dT%H%M%S)
TARGET="$BASE_DIR/base-$STAMP"
LOCK="$BASE_DIR/.lock"

mkdir -p "$BASE_DIR"

# Crude but sufficient single-writer lock (weekly cadence, tiny DB).
if [ -e "$LOCK" ]; then
    echo "pg-basebackup: lock present ($LOCK) — another run in progress? aborting" >&2
    exit 1
fi
trap 'rm -f "$LOCK"' EXIT
: > "$LOCK"

# -Ft -z: compressed tars (base.tar.gz + pg_wal.tar.gz).
# -X stream: the WAL spanning the backup rides inside it, so each base is
# restorable ALONE (plus archives for roll-forward past its end).
if ! "$PG_BIN/pg_basebackup" -h localhost -p 5432 -D "$TARGET" -Ft -z -X stream -c fast; then
    echo "pg-basebackup: backup failed — removing partial $TARGET" >&2
    rm -rf "$TARGET"
    exit 1
fi

# Sanity: a usable base has base.tar.gz with a backup_label inside.
if ! tar -xzOf "$TARGET/base.tar.gz" backup_label >/dev/null 2>&1; then
    echo "pg-basebackup: $TARGET has no readable backup_label — removing" >&2
    rm -rf "$TARGET"
    exit 1
fi

# Retention: newest KEEP bases stay, older go.
ls -d "$BASE_DIR"/base-* 2>/dev/null | sort | head -n -"$KEEP" | while read -r old; do
    echo "pg-basebackup: pruning old base $old"
    rm -rf "$old"
done

# Prune WAL older than the oldest KEPT base needs. The cutoff segment name
# comes from that base's own backup_label (START WAL LOCATION ... (file X)).
oldest=$(ls -d "$BASE_DIR"/base-* 2>/dev/null | sort | head -1)
if [ -n "$oldest" ] && [ -d "$WAL_DIR" ]; then
    cutoff=$(tar -xzOf "$oldest/base.tar.gz" backup_label 2>/dev/null \
        | sed -n 's/^START WAL LOCATION: .* (file \([0-9A-F]*\))$/\1/p')
    if [ -n "$cutoff" ]; then
        "$PG_BIN/pg_archivecleanup" -x .gz "$WAL_DIR" "$cutoff"
    else
        echo "pg-basebackup: could not read WAL cutoff from $oldest — skipping WAL prune (archives only grow, never break)" >&2
    fi
fi

echo "pg-basebackup: OK $TARGET (kept $(ls -d "$BASE_DIR"/base-* 2>/dev/null | wc -l | tr -d ' ') base(s))"
exit 0
