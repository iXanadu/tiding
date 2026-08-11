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
# crashloop-while-up, backup freshness, rotation drift, disk. One line per
# check (PASS/FAIL/SKIP + detail), exit non-zero if anything FAILs — usable
# by hand, by cron, or as an AgentBeast check_tools.d executable.
#
# Read-only: this script never mutates anything.

set -u

ENGRAM_URL="${ENGRAM_URL:-http://localhost:8920}"
PG_BIN="${PG_BIN:-/opt/homebrew/opt/postgresql@17/bin}"
PG_LOGS=("/opt/homebrew/var/log/postgresql@17.log" "/var/log/macmini/postgresql.err")
DUMP_DIR="${DUMP_DIR:-$HOME/.local/state/fleetbackup/dumps}"
DUMP_MAX_AGE_MIN="${DUMP_MAX_AGE_MIN:-120}"   # cadence is 30 min; 2h = 4 missed runs
DISK_MAX_PCT="${DISK_MAX_PCT:-90}"
CRASHLOOP_WINDOW_MIN="${CRASHLOOP_WINDOW_MIN:-10}"

fails=0
pass() { echo "PASS $1: $2"; }
fail() { echo "FAIL $1: $2"; fails=$((fails + 1)); }
skip() { echo "SKIP $1: $2"; }

# 1. engram service health — the store answers and says its deps are fine.
health=$(curl -s -m 8 "$ENGRAM_URL/health" 2>/dev/null)
if [ -z "$health" ]; then
    fail "engram-health" "no answer from $ENGRAM_URL/health"
elif echo "$health" | grep -q '"postgres":true' && echo "$health" | grep -q '"embeddings":true'; then
    pass "engram-health" "postgres+embeddings ok"
else
    fail "engram-health" "answered but degraded: $health"
fi

# 2. postgres accepting connections.
if [ -x "$PG_BIN/pg_isready" ]; then
    if "$PG_BIN/pg_isready" -h localhost -p 5432 -q; then
        pass "postgres-up" "accepting connections on 5432"
    else
        fail "postgres-up" "pg_isready reports not accepting"
    fi
else
    skip "postgres-up" "pg_isready not found at $PG_BIN (not a postgres host?)"
fi

# 3. single ownership (macOS): exactly one launchd job may own postgres.
#    The 45-day silent crashloop was two KeepAlive jobs sharing one datadir.
if [ "$(uname)" = "Darwin" ]; then
    daemon_loaded=0
    sudo -n launchctl print system/com.macmini.postgresql >/dev/null 2>&1 && daemon_loaded=1
    agent_plists=$(ls "$HOME/Library/LaunchAgents/" 2>/dev/null | grep -ci "postgres" || true)
    if [ "$daemon_loaded" -eq 1 ] && [ "$agent_plists" -eq 0 ]; then
        pass "postgres-owner" "system daemon only (no login-agent plist)"
    elif [ "$daemon_loaded" -eq 0 ] && [ "$agent_plists" -gt 0 ]; then
        fail "postgres-owner" "LOGIN AGENT owns postgres — headless reboot leaves the store down until someone logs in"
    elif [ "$daemon_loaded" -eq 1 ] && [ "$agent_plists" -gt 0 ]; then
        fail "postgres-owner" "TWO owners (daemon + $agent_plists agent plist(s)) — the Jun-27 silent-crashloop split"
    else
        # sudo -n may simply lack a cached credential; don't cry wolf.
        if sudo -n true 2>/dev/null; then
            fail "postgres-owner" "NO launchd job owns postgres"
        else
            skip "postgres-owner" "cannot inspect system domain without sudo"
        fi
    fi
else
    skip "postgres-owner" "macOS-only check"
fi

# 4. crashloop-while-up: repeated 'lock file already exists' in the last N min
#    means a second postgres is fighting the running one RIGHT NOW.
loop_hits=0
cutoff=$(date -v-"${CRASHLOOP_WINDOW_MIN}"M "+%Y-%m-%d %H:%M" 2>/dev/null || date -d "-${CRASHLOOP_WINDOW_MIN} min" "+%Y-%m-%d %H:%M")
for lg in "${PG_LOGS[@]}"; do
    [ -f "$lg" ] || continue
    hits=$(tail -200 "$lg" | awk -v c="$cutoff" '$0 ~ /lock file .* already exists/ { ts = $1 " " $2; if (ts >= c) n++ } END { print n+0 }')
    loop_hits=$((loop_hits + hits))
done
if [ "$loop_hits" -eq 0 ]; then
    pass "postgres-crashloop" "no lock-file fights in last ${CRASHLOOP_WINDOW_MIN}m"
else
    fail "postgres-crashloop" "$loop_hits lock-file failures in last ${CRASHLOOP_WINDOW_MIN}m — a second postgres is fighting the running one"
fi

# 5. backup freshness — the layer that DID detect the 21h outage, into a file
#    nobody read. Surface both its flag and the dump age.
if [ -d "$DUMP_DIR" ]; then
    if [ -f "$DUMP_DIR/STALE" ]; then
        fail "backup-fresh" "STALE flag present: $(head -1 "$DUMP_DIR/STALE")"
    else
        newest=$(find "$DUMP_DIR" -maxdepth 1 -name "engram-*.dump" -mmin -"$DUMP_MAX_AGE_MIN" 2>/dev/null | head -1)
        if [ -n "$newest" ]; then
            pass "backup-fresh" "dump younger than ${DUMP_MAX_AGE_MIN}m present"
        else
            fail "backup-fresh" "no engram dump younger than ${DUMP_MAX_AGE_MIN}m in $DUMP_DIR"
        fi
    fi
else
    skip "backup-fresh" "$DUMP_DIR absent (not the backup host?)"
fi

# 6. log-rotation drift (LOG-1): the conf install.sh intends must exist on THIS host.
if [ "$(uname)" = "Darwin" ]; then
    if [ -f /etc/newsyslog.d/engram.conf ]; then
        pass "log-rotation" "/etc/newsyslog.d/engram.conf present"
    else
        fail "log-rotation" "/etc/newsyslog.d/engram.conf MISSING — logs grow without bound (LOG-1 class)"
    fi
else
    skip "log-rotation" "macOS-only check"
fi

# 7. disk headroom on the datadir volume.
disk_pct=$(df -P /opt/homebrew/var 2>/dev/null | awk 'NR==2 { sub("%","",$5); print $5 }')
if [ -n "${disk_pct:-}" ]; then
    if [ "$disk_pct" -lt "$DISK_MAX_PCT" ]; then
        pass "disk" "datadir volume at ${disk_pct}%"
    else
        fail "disk" "datadir volume at ${disk_pct}% (threshold ${DISK_MAX_PCT}%)"
    fi
else
    skip "disk" "could not stat /opt/homebrew/var"
fi

if [ "$fails" -gt 0 ]; then
    echo "DOCTOR: $fails check(s) FAILED"
    exit 1
fi
echo "DOCTOR: all checks passed"
exit 0
