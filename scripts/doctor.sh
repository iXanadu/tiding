#!/bin/bash
# doctor.sh — verify the HOST actually matches what the install intends.
#
# Born from two incidents that no liveness check could see:
#   · 2026-06-27→08-11: TWO launchd jobs owned postgres on macmini (boot daemon
#     + brew login agent). The loser crash-looped every 10s for 45 days — while
#     the service stayed up, so every health check stayed green. After a power
#     cut the roles inverted and the box spent 21h with no store at all.
#   · LOG-1: install.sh writes /etc/newsyslog.d/engram.conf, but a box whose
#     install predated that step had nothing — engram.log grew to 176MB.
#     Config written as code is not config present on the host.
#
# So this script checks the classes a green /health hides: split ownership,
# crashloop-while-up, backup freshness, rotation drift, disk. Read-only —
# it never mutates anything.
#
# THREE MODES (the last two speak AgentBeast's fleet-check-contract-v1, the
# delivery leg the 21h incident showed was missing — detection that reaches
# a pager instead of a file):
#   doctor.sh                  human run: one PASS/FAIL/SKIP line per check,
#                              exit non-zero if anything FAILs
#   doctor.sh check-manifest   print the check-tool manifest JSON
#   doctor.sh check <name>     run one check, print {"status","detail"} JSON
#                              (status: ok|crit|unknown — unknown ≠ ok: it
#                              means "could not determine", and it pages)

set -u

ENGRAM_URL="${ENGRAM_URL:-http://localhost:8920}"
PG_BIN="${PG_BIN:-/opt/homebrew/opt/postgresql@17/bin}"
PG_LOGS=("/opt/homebrew/var/log/postgresql@17.log" "/var/log/macmini/postgresql.err")
DUMP_DIR="${DUMP_DIR:-$HOME/.local/state/fleetbackup/dumps}"
DUMP_MAX_AGE_MIN="${DUMP_MAX_AGE_MIN:-120}"   # cadence is 30 min; 2h = 4 missed runs
DISK_MAX_PCT="${DISK_MAX_PCT:-90}"
CRASHLOOP_WINDOW_MIN="${CRASHLOOP_WINDOW_MIN:-10}"

# Stable prod path for the manifest — the scheduler invokes THIS install.
SELF="/opt/srv/engram/scripts/doctor.sh"

CHECKS=(engram_health postgres_up postgres_owner postgres_crashloop backup_fresh log_rotation disk_headroom)

# Each check sets CH_STATUS (ok|crit|unknown) and CH_DETAIL.
CH_STATUS=""
CH_DETAIL=""

check_engram_health() {
    local health
    health=$(curl -s -m 8 "$ENGRAM_URL/health" 2>/dev/null)
    if [ -z "$health" ]; then
        CH_STATUS=crit; CH_DETAIL="no answer from $ENGRAM_URL/health"
    elif echo "$health" | grep -q '"postgres":true' && echo "$health" | grep -q '"embeddings":true'; then
        CH_STATUS=ok; CH_DETAIL="postgres+embeddings ok"
    else
        CH_STATUS=crit; CH_DETAIL="answered but degraded: $health"
    fi
}

check_postgres_up() {
    if [ -x "$PG_BIN/pg_isready" ]; then
        if "$PG_BIN/pg_isready" -h localhost -p 5432 -q; then
            CH_STATUS=ok; CH_DETAIL="accepting connections on 5432"
        else
            CH_STATUS=crit; CH_DETAIL="pg_isready reports not accepting"
        fi
    else
        CH_STATUS=unknown; CH_DETAIL="pg_isready not found at $PG_BIN"
    fi
}

# Single ownership (macOS): exactly one launchd job may own postgres.
# The 45-day silent crashloop was two KeepAlive jobs sharing one datadir.
check_postgres_owner() {
    if [ "$(uname)" != "Darwin" ]; then
        CH_STATUS=unknown; CH_DETAIL="macOS-only check"
        return
    fi
    # 'launchctl print system/<label>' is readable unprivileged (verified on
    # this box) — keep sudo -n only as a fallback so a scheduler context
    # without a sudo grant still gets a real answer, not a false page.
    local daemon_loaded=0 agent_plists
    if launchctl print system/com.macmini.postgresql >/dev/null 2>&1 \
        || sudo -n launchctl print system/com.macmini.postgresql >/dev/null 2>&1; then
        daemon_loaded=1
    fi
    agent_plists=$(ls "$HOME/Library/LaunchAgents/" 2>/dev/null | grep -ci "postgres" || true)
    if [ "$daemon_loaded" -eq 1 ] && [ "$agent_plists" -eq 0 ]; then
        CH_STATUS=ok; CH_DETAIL="system daemon only (no login-agent plist)"
    elif [ "$daemon_loaded" -eq 0 ] && [ "$agent_plists" -gt 0 ]; then
        CH_STATUS=crit; CH_DETAIL="LOGIN AGENT owns postgres — headless reboot leaves the store down until someone logs in"
    elif [ "$daemon_loaded" -eq 1 ] && [ "$agent_plists" -gt 0 ]; then
        CH_STATUS=crit; CH_DETAIL="TWO owners (daemon + $agent_plists agent plist(s)) — the Jun-27 silent-crashloop split"
    else
        CH_STATUS=crit; CH_DETAIL="NO launchd job owns postgres"
    fi
}

# Crashloop-while-up: repeated 'lock file already exists' in the last N min
# means a second postgres is fighting the running one RIGHT NOW.
check_postgres_crashloop() {
    local loop_hits=0 cutoff lg hits
    cutoff=$(date -v-"${CRASHLOOP_WINDOW_MIN}"M "+%Y-%m-%d %H:%M" 2>/dev/null || date -d "-${CRASHLOOP_WINDOW_MIN} min" "+%Y-%m-%d %H:%M")
    for lg in "${PG_LOGS[@]}"; do
        [ -f "$lg" ] || continue
        hits=$(tail -200 "$lg" | awk -v c="$cutoff" '$0 ~ /lock file .* already exists/ { ts = $1 " " $2; if (ts >= c) n++ } END { print n+0 }')
        loop_hits=$((loop_hits + hits))
    done
    if [ "$loop_hits" -eq 0 ]; then
        CH_STATUS=ok; CH_DETAIL="no lock-file fights in last ${CRASHLOOP_WINDOW_MIN}m"
    else
        CH_STATUS=crit; CH_DETAIL="$loop_hits lock-file failures in last ${CRASHLOOP_WINDOW_MIN}m — a second postgres is fighting the running one"
    fi
}

# Backup freshness — the layer that DID detect the 21h outage, into a file
# nobody read. Surface both its flag and the dump age.
check_backup_fresh() {
    if [ ! -d "$DUMP_DIR" ]; then
        CH_STATUS=unknown; CH_DETAIL="$DUMP_DIR absent (not the backup host?)"
        return
    fi
    if [ -f "$DUMP_DIR/STALE" ]; then
        CH_STATUS=crit; CH_DETAIL="STALE flag present: $(head -1 "$DUMP_DIR/STALE")"
    else
        local newest
        newest=$(find "$DUMP_DIR" -maxdepth 1 -name "engram-*.dump" -mmin -"$DUMP_MAX_AGE_MIN" 2>/dev/null | head -1)
        if [ -n "$newest" ]; then
            CH_STATUS=ok; CH_DETAIL="dump younger than ${DUMP_MAX_AGE_MIN}m present"
        else
            CH_STATUS=crit; CH_DETAIL="no engram dump younger than ${DUMP_MAX_AGE_MIN}m in $DUMP_DIR"
        fi
    fi
}

# Log-rotation drift (LOG-1): the conf install.sh intends must exist on THIS host.
check_log_rotation() {
    if [ "$(uname)" != "Darwin" ]; then
        CH_STATUS=unknown; CH_DETAIL="macOS-only check"
        return
    fi
    if [ -f /etc/newsyslog.d/engram.conf ]; then
        CH_STATUS=ok; CH_DETAIL="/etc/newsyslog.d/engram.conf present"
    else
        CH_STATUS=crit; CH_DETAIL="/etc/newsyslog.d/engram.conf MISSING — logs grow without bound (LOG-1 class)"
    fi
}

check_disk_headroom() {
    local disk_pct
    disk_pct=$(df -P /opt/homebrew/var 2>/dev/null | awk 'NR==2 { sub("%","",$5); print $5 }')
    if [ -z "${disk_pct:-}" ]; then
        CH_STATUS=unknown; CH_DETAIL="could not stat /opt/homebrew/var"
    elif [ "$disk_pct" -lt "$DISK_MAX_PCT" ]; then
        CH_STATUS=ok; CH_DETAIL="datadir volume at ${disk_pct}%"
    else
        CH_STATUS=crit; CH_DETAIL="datadir volume at ${disk_pct}% (threshold ${DISK_MAX_PCT}%)"
    fi
}

json_escape() {
    # Details are controlled strings, but STALE-file heads etc. can carry
    # arbitrary text — strip what would break a hand-rolled JSON literal.
    printf '%s' "$1" | tr -d '"\\' | tr '\n\t' '  '
}

emit_manifest() {
    cat <<MANIFEST
{
  "schema_version": 1,
  "tool": "engram-doctor",
  "summary": "engram store host health: the classes a green /health hides (split postgres ownership, crashloop-while-up, backup staleness, rotation drift, disk)",
  "node_scope": ["hub"],
  "checks": [
    {
      "name": "engram_health",
      "summary": "engram /health answers with postgres+embeddings ok",
      "invoke": {"binary": "$SELF", "args": ["check", "engram_health"], "timeout_s": 20, "requires_root": false},
      "interval_s": 300,
      "severity": "critical",
      "alert_on": ["crit", "unknown"],
      "dedup_window_s": 3600
    },
    {
      "name": "postgres_up",
      "summary": "postgres accepting connections on 5432",
      "invoke": {"binary": "$SELF", "args": ["check", "postgres_up"], "timeout_s": 20, "requires_root": false},
      "interval_s": 300,
      "severity": "critical",
      "alert_on": ["crit", "unknown"],
      "dedup_window_s": 3600
    },
    {
      "name": "postgres_owner",
      "summary": "exactly one launchd job (the system daemon) owns postgres",
      "invoke": {"binary": "$SELF", "args": ["check", "postgres_owner"], "timeout_s": 20, "requires_root": false},
      "interval_s": 900,
      "severity": "critical",
      "alert_on": ["crit", "unknown"],
      "dedup_window_s": 3600
    },
    {
      "name": "postgres_crashloop",
      "summary": "no lock-file fights in the recent window (a second postgres fighting the live one)",
      "invoke": {"binary": "$SELF", "args": ["check", "postgres_crashloop"], "timeout_s": 20, "requires_root": false},
      "interval_s": 300,
      "severity": "critical",
      "alert_on": ["crit", "unknown"],
      "dedup_window_s": 3600
    },
    {
      "name": "backup_fresh",
      "summary": "FleetBackup STALE flag absent and an engram dump younger than 2h exists",
      "invoke": {"binary": "$SELF", "args": ["check", "backup_fresh"], "timeout_s": 20, "requires_root": false},
      "interval_s": 900,
      "severity": "critical",
      "alert_on": ["crit", "unknown"],
      "dedup_window_s": 3600
    },
    {
      "name": "log_rotation",
      "summary": "newsyslog rotation conf present for engram logs (LOG-1 drift class)",
      "invoke": {"binary": "$SELF", "args": ["check", "log_rotation"], "timeout_s": 20, "requires_root": false},
      "interval_s": 3600,
      "severity": "warning",
      "alert_on": ["crit", "unknown"],
      "dedup_window_s": 21600
    },
    {
      "name": "disk_headroom",
      "summary": "datadir volume below ${DISK_MAX_PCT}%",
      "invoke": {"binary": "$SELF", "args": ["check", "disk_headroom"], "timeout_s": 20, "requires_root": false},
      "interval_s": 900,
      "severity": "critical",
      "alert_on": ["crit", "unknown"],
      "dedup_window_s": 21600
    }
  ]
}
MANIFEST
}

run_one() {
    local name="$1" fn="check_$1" known=0 c
    for c in "${CHECKS[@]}"; do [ "$c" = "$name" ] && known=1; done
    if [ "$known" -eq 0 ]; then
        printf '{"status": "unknown", "detail": "no such check: %s"}\n' "$(json_escape "$name")"
        return 0
    fi
    "$fn"
    printf '{"status": "%s", "detail": "%s"}\n' "$CH_STATUS" "$(json_escape "$CH_DETAIL")"
}

human_run() {
    local fails=0 name fn label
    for name in "${CHECKS[@]}"; do
        fn="check_$name"
        "$fn"
        case "$CH_STATUS" in
            ok)      label=PASS ;;
            crit)    label=FAIL; fails=$((fails + 1)) ;;
            unknown) label=SKIP ;;
        esac
        echo "$label $name: $CH_DETAIL"
    done
    if [ "$fails" -gt 0 ]; then
        echo "DOCTOR: $fails check(s) FAILED"
        exit 1
    fi
    echo "DOCTOR: all checks passed"
    exit 0
}

case "${1:-}" in
    "")             human_run ;;
    check-manifest) emit_manifest ;;
    check)          run_one "${2:-}" ;;
    *)              echo "usage: doctor.sh [check-manifest | check <name>]" >&2; exit 2 ;;
esac
