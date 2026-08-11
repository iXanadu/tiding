#!/bin/bash
# wal-archive.sh — postgres archive_command target for engram's DR-3 (PITR).
#
# Called by the postmaster as: wal-archive.sh <path-to-wal> <wal-filename>
# (postgresql.conf: archive_command = '.../wal-archive.sh "%p" "%f"')
#
# Contract with postgres (this is load-bearing, do not soften):
#   · exit 0  ONLY when the segment is durably archived — postgres then
#     recycles the WAL file and the archive is the ONLY copy.
#   · exit non-zero on ANY doubt — postgres keeps the segment in pg_wal and
#     retries forever. pg_wal grows while we fail, which the doctor's
#     archiver_ok check surfaces; growth-until-paged beats a silent gap in
#     the recovery chain.
#   · NEVER overwrite an existing archive: a duplicate filename with
#     different content means something is badly wrong (timeline confusion,
#     two clusters archiving to one dir) and must fail loudly.
#
# Archives land gzipped in the FleetBackup dump dir's wal/ subdir, so they
# ride the existing onsite+offsite shipping with zero new transport.
# Restore half: restore_command = 'gunzip -c <dir>/%f.gz > "%p"'
# (drilled end-to-end 2026-08-11 — see docs/design/dr3-pitr.md).

set -u

WAL_PATH="${1:?wal path (%p) required}"
WAL_FILE="${2:?wal filename (%f) required}"
ARCHIVE_DIR="/Users/ixanadu/.local/state/fleetbackup/dumps/wal"

mkdir -p "$ARCHIVE_DIR" || exit 1

TARGET="$ARCHIVE_DIR/$WAL_FILE.gz"
TMP="$TARGET.tmp.$$"

# Refuse to clobber. (A retry of a PARTIAL previous attempt leaves only a
# .tmp file, which we overwrite freely — only a completed archive is sacred.)
if [ -e "$TARGET" ]; then
    echo "wal-archive: $TARGET already exists — refusing to overwrite" >&2
    exit 1
fi

if ! gzip -c "$WAL_PATH" > "$TMP"; then
    rm -f "$TMP"
    echo "wal-archive: gzip of $WAL_FILE failed" >&2
    exit 1
fi

# mv within one filesystem is atomic — no reader ever sees a partial archive.
if ! mv "$TMP" "$TARGET"; then
    rm -f "$TMP"
    echo "wal-archive: finalize of $WAL_FILE failed" >&2
    exit 1
fi

exit 0
