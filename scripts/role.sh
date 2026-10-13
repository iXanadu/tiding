#!/usr/bin/env bash
# ROLE-1: owner-assigned roles — an address one agent holds at a time.
#
#   scripts/role.sh                       # list roles and holders
#   scripts/role.sh hand grokbot          # assign / hand over the role
#   scripts/role.sh --history hand        # who held it, when, set by whom
#   scripts/role.sh --retire hand         # remove the role
#
# A handover gives the new holder the role's OPEN mail; the old holder keeps
# its own mail. Auth: ENGRAM_ADMIN_TOKEN, else the `${ENGRAM_ADMIN_KEY:-janitor}=`
# line of the operator's custody file.
set -euo pipefail
KEYS="${ENGRAM_KEYS_FILE:-$HOME/.config/engram.keys}"
BASE="${ENGRAM_API_URL:-http://localhost:8920}"
TOKEN="${ENGRAM_ADMIN_TOKEN:-}"
if [ -z "$TOKEN" ] && [ -f "$KEYS" ]; then
    TOKEN=$(grep "^${ENGRAM_ADMIN_KEY:-janitor}=" "$KEYS" | head -1 | cut -d= -f2-)
fi
[ -n "$TOKEN" ] || { echo "no admin token (ENGRAM_ADMIN_TOKEN or $KEYS)" >&2; exit 2; }
api() { curl -sS -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' "$@"; }
pretty() { python3 -m json.tool; }
case "${1:-}" in
    "")          api "$BASE/admin/roles" | pretty ;;
    --history)   api "$BASE/admin/roles/$2/history" | pretty ;;
    --retire)    api -X DELETE "$BASE/admin/roles/$2" | pretty ;;
    *)           [ $# -eq 2 ] || { sed -n 2,11p "$0" >&2; exit 2; }
                 api -X PUT "$BASE/admin/roles/$1" -d "{\"holder\": \"$2\"}" | pretty ;;
esac
