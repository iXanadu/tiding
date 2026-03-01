#!/bin/bash
# Stop engram service
#
# Usage:
#   ./scripts/stop.sh

set -e

LABEL="com.engram"

if [[ "$(uname)" == "Darwin" ]]; then
    PLIST="/Library/LaunchDaemons/${LABEL}.plist"
    if ! sudo launchctl list "$LABEL" &>/dev/null; then
        echo "Service not running."
        exit 0
    fi

    sudo launchctl unload "$PLIST"
    echo "Service stopped"

elif [[ "$(uname)" == "Linux" ]]; then
    sudo systemctl stop engram
    echo "Service stopped"
fi
