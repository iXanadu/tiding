#!/usr/bin/env bash
#
# backup-db.sh — dump the engram database to a restorable, verified archive.
#
# WHY THIS EXISTS
#   On 2026-07-23 a single bad admin call deleted 1733 rows and there was no
#   restore path of any kind: no dump, archive_mode=off, no PITR. Memory
#   survived only because the delete predicate happened not to match it. A
#   pg_dump pre-backup hook was decided on 2026-06-14 and never implemented.
#   This is that implementation.
#
# DIVISION OF LABOUR
#   engram produces a correct, restorable, VERIFIED dump. FleetBackup gets it
#   offsite and alerts when it doesn't happen. This script deliberately does no
#   scheduling, no rotation offsite, and no uploading — that is the harness's
#   job and duplicating it would create two things to keep right.
#
# CONTRACT
#   exit 0  → a dump exists at $OUT_DIR, is non-empty, and pg_restore can read
#             its table of contents. Path is echoed on stdout as the LAST line.
#   exit !0 → NO usable dump was produced. Diagnostics on stderr.
#
#   A caller MUST treat non-zero as "do not proceed with a snapshot that claims
#   to contain a database". Silently snapshotting a stale dump reproduces the
#   exact failure this exists to prevent: a backup that looks present and isn't.
#
# Usage: backup-db.sh [--out-dir DIR] [--keep N] [--quiet]

set -euo pipefail

OUT_DIR="${ENGRAM_BACKUP_DIR:-/opt/srv/engram-backups}"
KEEP="${ENGRAM_BACKUP_KEEP:-7}"
QUIET=0
ENV_FILE="${ENGRAM_ENV_FILE:-/opt/srv/engram/.env}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out-dir) OUT_DIR="$2"; shift 2 ;;
    --keep)    KEEP="$2";    shift 2 ;;
    --quiet)   QUIET=1;      shift ;;
    --env-file) ENV_FILE="$2"; shift 2 ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "backup-db: unknown argument '$1'" >&2; exit 2 ;;
  esac
done

log () { [[ $QUIET -eq 1 ]] || echo "backup-db: $*" >&2; }
die () { echo "backup-db: FATAL — $*" >&2; exit 1; }

# --- credentials -----------------------------------------------------------
# Read from the server's own .env so there is exactly ONE place DB credentials
# live. A backup script with its own copy of the credentials drifts silently
# and then backs up the wrong database — which is indistinguishable from
# working right up until you need it.
[[ -r "$ENV_FILE" ]] || die "cannot read env file '$ENV_FILE' (set --env-file or ENGRAM_ENV_FILE)"

get_env () {
  # Value = everything after the first '='. Then:
  #   1. strip an inline comment ONLY when preceded by whitespace, so a value
  #      that legitimately contains '#' (passwords do) survives intact;
  #   2. trim surrounding whitespace — this box's .env pads values with a long
  #      run of trailing spaces, which silently produced the role
  #      "ixanadu                    " and a failed connection;
  #   3. unwrap matching quotes.
  sed -n "s/^[[:space:]]*$1[[:space:]]*=//p" "$ENV_FILE" | head -1 \
    | sed 's/[[:space:]][[:space:]]*#.*$//' \
    | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' \
    | sed 's/^"\(.*\)"$/\1/' | sed "s/^'\(.*\)'$/\1/"
}

DB_HOST="$(get_env ENGRAM_DB_HOST)"; DB_HOST="${DB_HOST:-localhost}"
DB_PORT="$(get_env ENGRAM_DB_PORT)"; DB_PORT="${DB_PORT:-5432}"
DB_NAME="$(get_env ENGRAM_DB_NAME)"; DB_NAME="${DB_NAME:-engram}"
DB_USER="$(get_env ENGRAM_DB_USER)"; DB_USER="${DB_USER:-$(whoami)}"
DB_PASS="$(get_env ENGRAM_DB_PASSWORD)"

# --- locate postgres client tools -----------------------------------------
# A hook runs under launchd/cron with a minimal PATH that will NOT include
# homebrew. Discovering the tools here — rather than assuming an interactive
# shell's PATH — is the difference between a backup that runs unattended and
# one that only ever worked when a human tried it.
if ! command -v pg_dump >/dev/null; then
  for d in /opt/homebrew/opt/postgresql@17/bin /opt/homebrew/opt/postgresql@16/bin \
           /opt/homebrew/bin /usr/local/opt/postgresql@17/bin /usr/local/bin \
           /usr/lib/postgresql/17/bin /usr/lib/postgresql/16/bin /usr/bin; do
    if [[ -x "$d/pg_dump" ]]; then PATH="$d:$PATH"; break; fi
  done
fi
export PATH
command -v pg_dump    >/dev/null || die "pg_dump not found (searched PATH and the usual postgres install dirs)"
command -v pg_restore >/dev/null || die "pg_restore not found (needed to VERIFY the dump)"

mkdir -p "$OUT_DIR" || die "cannot create '$OUT_DIR'"
chmod 700 "$OUT_DIR" 2>/dev/null || true

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TMP="$OUT_DIR/.engram-$STAMP.dump.partial"
OUT="$OUT_DIR/engram-$STAMP.dump"

cleanup () { rm -f "$TMP"; }
trap cleanup EXIT

export PGPASSWORD="$DB_PASS"

# --- dump ------------------------------------------------------------------
# -Fc (custom format): compressed, and pg_restore can list/extract INDIVIDUAL
# tables from it. That matters here — the realistic disaster is "one table got
# wiped", not "the disk died", and plain SQL would force an all-or-nothing
# restore over a live database.
log "dumping ${DB_NAME}@${DB_HOST}:${DB_PORT} -> $OUT"
if ! pg_dump --format=custom --compress=6 --no-owner --no-privileges \
             --host="$DB_HOST" --port="$DB_PORT" --username="$DB_USER" \
             --dbname="$DB_NAME" --file="$TMP" 2>/tmp/engram-pgdump-err.$$; then
  sed 's/^/backup-db:   /' /tmp/engram-pgdump-err.$$ >&2 || true
  rm -f /tmp/engram-pgdump-err.$$
  die "pg_dump failed — NO usable backup was produced"
fi
rm -f /tmp/engram-pgdump-err.$$

[[ -s "$TMP" ]] || die "pg_dump produced an empty file — NO usable backup"

# --- verify at creation, not at restore ------------------------------------
# The whole lesson of 2026-07-23 is that an untested recovery path is not a
# recovery path. Reading the archive's table of contents proves the file is a
# well-formed, non-truncated pg_dump rather than 40MB of nothing.
if ! pg_restore --list "$TMP" >/tmp/engram-toc.$$ 2>/dev/null; then
  rm -f /tmp/engram-toc.$$
  die "dump is unreadable by pg_restore (truncated or corrupt) — NOT keeping it"
fi

# The memories table is the fleet's actual asset; a dump without it is a
# well-formed file that would have saved nobody.
if ! grep -qi "TABLE DATA.* memories" /tmp/engram-toc.$$; then
  rm -f /tmp/engram-toc.$$
  die "dump contains no 'memories' table data — refusing to keep a useless backup"
fi
TOC_LINES=$(wc -l < /tmp/engram-toc.$$ | tr -d ' ')
rm -f /tmp/engram-toc.$$

mv "$TMP" "$OUT"
chmod 600 "$OUT"
trap - EXIT

# --- manifest: ground truth that travels WITH the dump ---------------------
# A restorer is told to "compare row counts against live" — but in the disaster
# this exists for, THERE IS NO LIVE DATABASE to compare against. Without a
# reference captured at dump time, a restore that silently loses half its rows
# verifies as "thousands, not zero" and passes.
#
# So record the counts here, beside the dump, where they ride into the same
# snapshot. Best-effort: a manifest failure must never invalidate a good dump.
MANIFEST="${OUT%.dump}.manifest"
if command -v psql >/dev/null; then
  {
    echo "# engram backup manifest — counts captured from the SOURCE at dump time."
    echo "# A restore that does not match these numbers is incomplete, even if it is non-empty."
    echo "dump_file=$(basename "$OUT")"
    echo "captured_utc=$STAMP"
    echo "source_db=${DB_NAME}@${DB_HOST}:${DB_PORT}"
    for pair in \
      "memories_total|select count(*) from memories" \
      "memories_inbox|select count(*) from memories where key like 'inbox/%'" \
      "memories_embedded|select count(*) from memories where embedding is not null" \
      "principals|select count(*) from principals" \
      "principal_aliases|select count(*) from principal_aliases"; do
      k="${pair%%|*}"; q="${pair#*|}"
      v=$(psql --host="$DB_HOST" --port="$DB_PORT" --username="$DB_USER" \
               --dbname="$DB_NAME" --no-psqlrc -tAq -c "$q" 2>/dev/null | tr -d ' ')
      echo "${k}=${v:-unknown}"
    done
  } > "$MANIFEST" 2>/dev/null && chmod 600 "$MANIFEST" \
    && log "manifest written: $(basename "$MANIFEST")" \
    || log "WARNING: manifest could not be written (dump itself is still good)"
else
  log "WARNING: psql not found — no manifest written, restore cannot self-verify"
fi

SIZE=$(du -h "$OUT" | cut -f1 | tr -d ' ')
log "ok — $SIZE, $TOC_LINES archive entries, verified readable"

# --- local rotation --------------------------------------------------------
# Keep a few generations so a bad dump can never overwrite the last good one.
# Offsite retention is FleetBackup's concern, not ours.
# NOTE: no `mapfile` — macOS ships bash 3.2 and this script must run under the
# system bash, not just a homebrew one that happens to be first on PATH.
if [[ "$KEEP" -gt 0 ]]; then
  ls -1t "$OUT_DIR"/engram-*.dump 2>/dev/null | tail -n +$((KEEP + 1)) | while IFS= read -r f; do
    [[ -n "$f" ]] && rm -f "$f" && log "rotated out $(basename "$f")"
  done
fi

# Path on stdout as the last line, so a hook can capture it.
echo "$OUT"
