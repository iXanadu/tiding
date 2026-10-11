#!/usr/bin/env bash
# PROJECT-RENAME-1: merge old project names into the current one.
#
#   scripts/project-relabel.sh <target> <source> [<source> ...]           # dry run
#   scripts/project-relabel.sh --apply <target> <source> [<source> ...]   # do it
#   scripts/project-relabel.sh --reverse <target> <source>                # undo one source
#   scripts/project-relabel.sh --list                                     # current aliases
#
# Source ORDER is precedence: when several names hold the same key (say
# startup/next), the target's own row wins, then the first source listed;
# the rest are kept under archive/<source>/<key>. List the name whose
# handoff is live first.
#
# Auth: an ADMIN token — ENGRAM_ADMIN_TOKEN, else the `${ENGRAM_ADMIN_KEY:-janitor}=`
# line of the operator's custody file (never stored here).
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
json_list() { python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "$@"; }

case "${1:-}" in
    --list)
        api "$BASE/admin/projects/aliases" | pretty ;;
    --reverse)
        [ $# -eq 3 ] || { echo "usage: $0 --reverse <target> <source>" >&2; exit 2; }
        api -X POST "$BASE/admin/projects/relabel/reverse" \
            -d "{\"target\": \"$2\", \"source\": \"$3\"}" | pretty ;;
    --apply|"")
        apply=false
        if [ "${1:-}" = "--apply" ]; then apply=true; shift; fi
        [ $# -ge 2 ] || { sed -n 2,12p "$0" >&2; exit 2; }
        target=$1; shift
        api -X POST "$BASE/admin/projects/relabel" \
            -d "{\"target\": \"$target\", \"sources\": $(json_list "$@"), \"dry_run\": $( $apply && echo false || echo true )}" | pretty ;;
    *)
        target=$1; shift
        api -X POST "$BASE/admin/projects/relabel" \
            -d "{\"target\": \"$target\", \"sources\": $(json_list "$@"), \"dry_run\": true}" | pretty ;;
esac
