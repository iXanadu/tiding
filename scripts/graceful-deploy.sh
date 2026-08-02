#!/usr/bin/env bash
# DEPLOY-2 — deploy engram without hard-killing in-flight requests.
#
# `scripts/restart.sh` uses `launchctl kickstart -k`, which SIGKILLs. Engram is
# fleet infrastructure: every agent on every box reads and writes through it, so
# a hard kill drops whatever was mid-flight for all of them. Six of those on
# 2026-08-01, each ~4s of fleet-wide refusal, while the owner had live sessions
# on other projects.
#
# This sends SIGTERM instead. uvicorn stops accepting, finishes in-flight
# requests, then exits; launchd (KeepAlive=true) restarts it. Same restart, but
# requests that were already running get to finish.
#
# It also does what the old script did not: state who is live BEFORE acting,
# measure the actual gap, and verify the thing it just deployed is serving.
#
#   scripts/graceful-deploy.sh              # pull + graceful restart + verify
#   scripts/graceful-deploy.sh --no-pull    # restart only (prod already at target)
#   scripts/graceful-deploy.sh --dry-run    # show what would happen, touch nothing
set -uo pipefail

APP_DIR="${ENGRAM_APP_DIR:-/opt/srv/engram}"
LABEL="com.engram"
HEALTH="http://localhost:8920/health"
PULL=1; DRY=0
for a in "$@"; do
    case "$a" in
        --no-pull) PULL=0 ;;
        --dry-run) DRY=1 ;;
        *) echo "unknown arg: $a" >&2; exit 2 ;;
    esac
done

say() { printf '%s\n' "$*"; }
die() { printf '⛔ %s\n' "$*" >&2; exit 1; }

# --- 1. Who is live? Stated, not assumed -------------------------------------
# Not a gate: a restart is legitimate with sessions running. But "I did not know
# anyone was there" and "I decided it was worth it" are different, and only the
# second is defensible after the fact.
say "── live sessions ──"
if command -v psql >/dev/null 2>&1; then
    psql -d engram -t -A -F' · ' -c "
        SELECT key, round(EXTRACT(EPOCH FROM (NOW()-last_used_at)))||'s ago'
        FROM memories WHERE scope='presence'
          AND last_used_at > NOW() - interval '10 minutes'
        ORDER BY last_used_at DESC;" 2>/dev/null | sed 's/^/  /' || say "  (roster unavailable)"
else
    say "  (psql not on PATH — cannot list; proceeding blind)"
fi

# --- 2. Rollback target, known BEFORE we move ---------------------------------
cd "$APP_DIR" || die "no $APP_DIR"
[[ -n "$(git status --porcelain)" ]] && die "prod tree is dirty — refusing to deploy over local changes"
PREV="$(git rev-parse --short HEAD)"
say ""
say "── target ──"
say "  rollback SHA : $PREV   (git checkout $PREV && rerun this script --no-pull)"

if [[ $PULL -eq 1 ]]; then
    if [[ $DRY -eq 1 ]]; then
        git fetch -q origin 2>/dev/null || true
        say "  would pull   : $PREV → $(git rev-parse --short origin/main 2>/dev/null || echo '?')"
    else
        git pull --ff-only -q || die "ff-only pull failed — prod has diverged, resolve by hand"
        say "  now at       : $(git rev-parse --short HEAD)"
    fi
fi

if [[ $DRY -eq 1 ]]; then
    say ""
    say "dry run — nothing was restarted."
    exit 0
fi

# --- 3. Graceful stop ---------------------------------------------------------
PID="$(sudo launchctl print "system/${LABEL}" 2>/dev/null | awk -F'= ' '/^[[:space:]]*pid =/{print $2; exit}')"
[[ -z "${PID:-}" ]] && die "could not find a running pid for ${LABEL}"
say ""
say "── restart ──"
say "  SIGTERM → pid $PID (uvicorn drains in-flight requests, then exits)"

START=$(date +%s)
sudo kill -TERM "$PID" 2>/dev/null || die "could not signal $PID"

# Wait for the old process to actually go. If it will not drain, say so rather
# than escalating silently — an unexplained hang is worth a human's attention
# more than an on-time restart.
for _ in $(seq 1 30); do
    kill -0 "$PID" 2>/dev/null || break
    sleep 1
done
if kill -0 "$PID" 2>/dev/null; then
    die "pid $PID still alive 30s after SIGTERM — NOT escalating to SIGKILL. Investigate; the service is still serving."
fi

# --- 4. launchd (KeepAlive=true) brings it back; wait for READY, not for a pid -
for _ in $(seq 1 60); do
    curl -sf "$HEALTH" >/dev/null 2>&1 && break
    sleep 1
done
END=$(date +%s)

HEALTH_JSON="$(curl -s "$HEALTH" 2>/dev/null)"
case "$HEALTH_JSON" in
    *'"status":"ok"'*) say "  back in $((END-START))s — $HEALTH_JSON" ;;
    *) die "unhealthy after $((END-START))s: ${HEALTH_JSON:-<no response>}  → rollback: git checkout $PREV && $0 --no-pull" ;;
esac

# --- 5. Prove it is SERVING, not merely up ------------------------------------
# A 200 on /health says the process started. It does not say the store answers.
# Every deploy this script exists for was a change to what engram serves.
say ""
say "── verify ──"
say "  running : $(git rev-parse --short HEAD)"
say "  serving : $(curl -s "$HEALTH" | tr -d '\n')"
say ""
say "✓ deployed. Confirm the specific thing you shipped is live — health is not that check."
