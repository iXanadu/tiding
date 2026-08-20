#!/bin/bash
# Start engram service
#
# Usage:
#   ./scripts/start.sh

set -e

LABEL="com.engram"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(dirname "$SCRIPT_DIR")"

# Preflight doctor: catch a self-refusing bind or a Host allowlist that won't
# cover how clients reach this box (the Tailscale/remote-reach class) BEFORE
# starting. A FAIL means the service would refuse to boot — stop and fix it.
PF_PY="$("$SCRIPT_DIR/resolve-venv-python.sh" engram-3.12 python 2>/dev/null)" || \
    PF_PY="$(command -v python3 || echo python)"
if ! (cd "$APP_DIR" && "$PF_PY" -m server.preflight); then
    echo
    echo "Preflight found a blocking problem (above). Fix it, then re-run start.sh."
    echo "(To start anyway: ENGRAM_SKIP_PREFLIGHT=1 ./scripts/start.sh)"
    [ "${ENGRAM_SKIP_PREFLIGHT:-}" = "1" ] || exit 1
fi
echo

if [[ "$(uname)" == "Darwin" ]]; then
    PLIST="/Library/LaunchDaemons/${LABEL}.plist"
    if [ ! -f "$PLIST" ]; then
        echo "ERROR: Plist not found at $PLIST"
        echo "Run ./scripts/install.sh first"
        exit 1
    fi

    # Check if already loaded
    if sudo launchctl list "$LABEL" &>/dev/null; then
        echo "Service already running. Use ./scripts/restart.sh to restart."
        exit 0
    fi

    sudo launchctl load "$PLIST"
    echo "Service started"

elif [[ "$(uname)" == "Linux" ]]; then
    sudo systemctl start engram
    echo "Service started"
fi

# Wait and verify — poll, don't sleep-once. Same reason as restart.sh: the
# embedding model loads during startup, so a single 2s probe reports failure
# on a service that is merely still booting.
HEALTH_TIMEOUT="${ENGRAM_HEALTH_TIMEOUT:-45}"
healthy=0
for ((i = 0; i < HEALTH_TIMEOUT; i++)); do
    if curl -sf http://localhost:8920/health > /dev/null 2>&1; then
        healthy=1
        break
    fi
    sleep 1
done
if [[ "$healthy" == "1" ]]; then
    echo "Health check: OK (${i}s)"
else
    echo "WARNING: still unhealthy after ${HEALTH_TIMEOUT}s — check logs"
    if [[ "$(uname)" == "Darwin" ]]; then
        SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
        APP_DIR="$(dirname "$SCRIPT_DIR")"
        echo "  tail -f $APP_DIR/logs/engram.err"
    else
        echo "  journalctl -u engram -f"
    fi
fi
