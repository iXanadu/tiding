#!/usr/bin/env bash
# Inbox janitor — invokes the two admin maintenance passes (build-plan
# Steps 13/14): climb (unhandled asks rise toward the living) and sweep
# (deep chatter expires with its epoch). Both are idempotent and serve
# skip ledgers; running them hourly is safe by construction.
#
# Auth: the dedicated `janitor` ADMIN principal (JANITOR-1, owner-approved
# 2026-08-18), read at runtime from the operator's custody file — never
# stored here (repo is written as if public). The owner bearer no longer
# rides the cron.
#
# Install (cron, hourly):
#   17 * * * * /opt/srv/engram/scripts/inbox-janitor.sh >> "$HOME/Library/Logs/engram-janitor.log" 2>&1
set -euo pipefail

KEYS="${ENGRAM_KEYS_FILE:-$HOME/.config/engram.keys}"
BASE="${ENGRAM_API_URL:-http://localhost:8920}"
TOKEN=$(grep '^janitor=' "$KEYS" | head -1 | cut -d= -f2-)
if [ -z "$TOKEN" ]; then
    echo "$(date -u +%FT%TZ) janitor: no janitor token in $KEYS — aborting" >&2
    exit 1
fi

for pass in climb sweep; do
    out=$(curl -sS -m 60 -X POST -H "Authorization: Bearer $TOKEN" \
          -H "Content-Type: application/json" -d '' \
          "$BASE/admin/inbox/$pass") || {
        echo "$(date -u +%FT%TZ) janitor: $pass FAILED" >&2
        continue
    }
    # One line per pass: enough to audit, never the row bodies.
    echo "$(date -u +%FT%TZ) janitor $pass: $(printf '%s' "$out" | head -c 400)"
done
