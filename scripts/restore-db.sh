#!/usr/bin/env bash
#
# restore-db.sh — restore an engram dump, and REHEARSE that restore safely.
#
# DESIGN PRINCIPLE, learned the hard way on 2026-07-23:
#   The incident that made this repo need backups was caused by a destructive
#   operation that was EASIER TO RUN THAN TO VERIFY. So the default mode here
#   is the safe one: restoring into a scratch database requires no ceremony,
#   and touching the live database requires deliberate, explicit targeting plus
#   a typed confirmation. You cannot reach production by forgetting a flag.
#
# MODES
#   --rehearse            restore into a scratch DB, verify, report row counts,
#                         then DROP the scratch DB. Proves the backup works.
#                         This is the default.
#   --into <dbname>       restore into a named (non-production) database and
#                         KEEP it, e.g. to extract specific rows by hand.
#   --production          restore over the live database. Requires --i-understand
#                         and an interactive typed confirmation.
#
# Usage:
#   restore-db.sh [--file DUMP] [--rehearse]
#   restore-db.sh --file DUMP --into engram_forensic
#   restore-db.sh --file DUMP --production --i-understand

set -euo pipefail

ENV_FILE="${ENGRAM_ENV_FILE:-/opt/srv/engram/.env}"
BACKUP_DIR="${ENGRAM_BACKUP_DIR:-/opt/srv/engram-backups}"
DUMP=""
MODE="rehearse"
TARGET=""
UNDERSTAND=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --file)        DUMP="$2"; shift 2 ;;
    --rehearse)    MODE="rehearse"; shift ;;
    --into)        MODE="into"; TARGET="$2"; shift 2 ;;
    --production)  MODE="production"; shift ;;
    --i-understand) UNDERSTAND=1; shift ;;
    --env-file)    ENV_FILE="$2"; shift 2 ;;
    -h|--help)     sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "restore-db: unknown argument '$1'" >&2; exit 2 ;;
  esac
done

log () { echo "restore-db: $*" >&2; }
die () { echo "restore-db: FATAL — $*" >&2; exit 1; }

[[ -r "$ENV_FILE" ]] || die "cannot read env file '$ENV_FILE'"
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
export PGPASSWORD="$(get_env ENGRAM_DB_PASSWORD)"

# Same PATH discovery as backup-db.sh — a restore is most likely to be run in
# an emergency, possibly from a bare shell, and "psql not found" is the worst
# possible thing to discover at that moment.
if ! command -v psql >/dev/null; then
  for d in /opt/homebrew/opt/postgresql@17/bin /opt/homebrew/opt/postgresql@16/bin \
           /opt/homebrew/bin /usr/local/opt/postgresql@17/bin /usr/local/bin \
           /usr/lib/postgresql/17/bin /usr/lib/postgresql/16/bin /usr/bin; do
    if [[ -x "$d/psql" ]]; then PATH="$d:$PATH"; break; fi
  done
fi
export PATH
command -v psql       >/dev/null || die "psql not found"
command -v pg_restore >/dev/null || die "pg_restore not found"

PSQL=(psql --host="$DB_HOST" --port="$DB_PORT" --username="$DB_USER" --no-psqlrc -tAq)

# Newest dump by default — the common case in an emergency is "restore the
# latest", and making someone construct a filename under stress invites typos.
if [[ -z "$DUMP" ]]; then
  DUMP="$(ls -1t "$BACKUP_DIR"/engram-*.dump 2>/dev/null | head -1 || true)"
  [[ -n "$DUMP" ]] || die "no dump found in '$BACKUP_DIR' — pass --file"
  log "using newest dump: $DUMP"
fi
[[ -r "$DUMP" ]] || die "cannot read dump '$DUMP'"
pg_restore --list "$DUMP" >/dev/null 2>&1 || die "'$DUMP' is not a readable pg_dump archive"

case "$MODE" in
  rehearse) TARGET="engram_restore_rehearsal_$$" ;;
  into)
    [[ -n "$TARGET" ]] || die "--into requires a database name"
    [[ "$TARGET" != "$DB_NAME" ]] || die "--into must NOT name the live database ('$DB_NAME'); use --production"
    ;;
  production)
    TARGET="$DB_NAME"
    [[ $UNDERSTAND -eq 1 ]] || die "--production also requires --i-understand"
    echo "" >&2
    echo "  ⛔ You are about to OVERWRITE the live engram database '$DB_NAME'" >&2
    echo "     on $DB_HOST:$DB_PORT with $DUMP" >&2
    echo "     Everything written since that dump will be LOST." >&2
    echo "     Stop the engram service first, or writes will race the restore." >&2
    echo "" >&2
    read -r -p "  Type the database name to proceed: " TYPED
    [[ "$TYPED" == "$DB_NAME" ]] || die "confirmation did not match — nothing was changed"
    ;;
esac

# --- restore ---------------------------------------------------------------
if [[ "$MODE" != "production" ]]; then
  log "creating scratch database '$TARGET'"
  "${PSQL[@]}" --dbname=postgres -c "DROP DATABASE IF EXISTS \"$TARGET\";" >/dev/null
  "${PSQL[@]}" --dbname=postgres -c "CREATE DATABASE \"$TARGET\";" >/dev/null
fi

log "restoring into '$TARGET' (this may take a minute)"
# pgvector/pg_trgm extensions and ownership noise are expected on a fresh DB;
# --no-owner keeps it clean. Errors are surfaced but a partial restore still
# gets verified below rather than being assumed good.
set +e
pg_restore --host="$DB_HOST" --port="$DB_PORT" --username="$DB_USER" \
           --dbname="$TARGET" --no-owner --no-privileges "$DUMP" 2>/tmp/engram-restore-err.$$
RC=$?
set -e
if [[ $RC -ne 0 ]]; then
  log "pg_restore exited $RC — showing errors (some are benign on a fresh DB):"
  sed 's/^/restore-db:   /' /tmp/engram-restore-err.$$ | head -20 >&2
fi
rm -f /tmp/engram-restore-err.$$

# --- verify by counting, not by trusting the exit code ---------------------
count () { "${PSQL[@]}" --dbname="$1" -c "$2" 2>/dev/null | tr -d ' ' || echo "ERR"; }

R_MEM=$(count "$TARGET" "select count(*) from memories;")
R_INBOX=$(count "$TARGET" "select count(*) from memories where key like 'inbox/%';")
R_PRIN=$(count "$TARGET" "select count(*) from principals;")

echo "" >&2
echo "  RESTORED CONTENTS of $TARGET" >&2
echo "    memories (total) : $R_MEM" >&2
echo "    ├─ inbox rows    : $R_INBOX" >&2
echo "    └─ knowledge rows: $(( ${R_MEM:-0} - ${R_INBOX:-0} ))" >&2
echo "    principals       : $R_PRIN" >&2

# --- compare against the manifest, not against "live" ----------------------
# In a real disaster the live database is GONE, so "compare to live" is advice
# you cannot follow. The manifest captured at dump time is the only reference
# that survives with the backup — prefer it, and fall back to live only when
# it is absent (older dumps predate manifests).
MANIFEST="${DUMP%.dump}.manifest"
VERDICT_FAIL=0
if [[ -r "$MANIFEST" ]]; then
  m_get () { sed -n "s/^$1=//p" "$MANIFEST" | head -1; }
  E_MEM=$(m_get memories_total); E_PRIN=$(m_get principals); E_EMB=$(m_get memories_embedded)
  R_EMB=$(count "$TARGET" "select count(*) from memories where embedding is not null;")
  echo "" >&2
  echo "  VERIFIED AGAINST MANIFEST (captured $(m_get captured_utc))" >&2
  chk () { # name expected actual
    if [[ "$2" == "unknown" || -z "$2" ]]; then echo "    $1: no reference in manifest" >&2
    elif [[ "$2" == "$3" ]]; then echo "    $1: $3 == $2  MATCH" >&2
    else echo "    $1: $3 != $2  ✗ MISMATCH" >&2; VERDICT_FAIL=1; fi
  }
  chk "memories  " "$E_MEM"  "$R_MEM"
  chk "principals" "$E_PRIN" "$R_PRIN"
  # Embeddings are what make semantic search work; rows without them restore
  # "successfully" into a memory system that cannot actually recall anything.
  chk "embeddings" "$E_EMB"  "$R_EMB"
else
  echo "    (no manifest beside this dump — cannot self-verify)" >&2
fi

if [[ "$MODE" == "rehearse" ]]; then
  L_MEM=$(count "$DB_NAME" "select count(*) from memories;")
  echo "    live DB, for reference: $L_MEM memories (expect drift — writes continue after a dump)" >&2
  echo "" >&2
  log "dropping rehearsal database '$TARGET'"
  "${PSQL[@]}" --dbname=postgres -c "DROP DATABASE IF EXISTS \"$TARGET\";" >/dev/null
  if [[ "$R_MEM" == "ERR" || "${R_MEM:-0}" -eq 0 ]]; then
    die "REHEARSAL FAILED — restored database has no memories. This backup would NOT have saved you."
  fi
  if [[ $VERDICT_FAIL -eq 1 ]]; then
    die "REHEARSAL FAILED — restored counts do not match the manifest. The dump is INCOMPLETE, not merely non-empty."
  fi
  log "REHEARSAL PASSED — '$DUMP' is a working, restorable backup."
fi
