#!/bin/bash
# Restart engram service
#
# Usage:
#   ./scripts/restart.sh

set -e

LABEL="com.engram"

if [[ "$(uname)" == "Darwin" ]]; then
    if ! sudo launchctl list "$LABEL" &>/dev/null; then
        echo "Service not running. Use ./scripts/start.sh to start."
        exit 1
    fi

    sudo launchctl kickstart -k "system/${LABEL}"
    echo "Service restarted"

elif [[ "$(uname)" == "Linux" ]]; then
    sudo systemctl restart engram
    echo "Service restarted"
fi

# Wait and verify. POLL, don't sleep-once: startup loads the embedding model
# (several seconds on MPS, longer on a cold cache), so a single 2s probe cried
# WARNING on virtually every healthy restart. An alarm that is usually wrong
# trains the next operator to ignore the one time it is right, which is worse
# than printing nothing at all.
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
