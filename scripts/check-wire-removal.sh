#!/usr/bin/env bash
# WIRE-1 — is this response field safe to remove?
#
# On 2026-08-01 removing `state` from /memory/roster broke `memory_roster` for
# every ALREADY-RUNNING session for 2h19m. The shipped bridge renders it as
# f"{e['state']:<15}" — a DIRECT SUBSCRIPT, which raises KeyError the moment the
# field stops arriving. Bridge changes only land at a session's NEXT start, so
# "the bridge no longer reads it" fixes nobody who is already up.
#
# The removal had been cleared by the one peer consumer who asked for it. That
# was mistaken for fleet clearance. THE BRIDGE IS ALSO A CONSUMER, and every
# running session holds an OLD COPY of it. A wire contract has as many consumers
# as there are DEPLOYED READERS, not as many as there are maintainers who answer.
#
# This script is the thirty-second check that would have caught it.
#
#   scripts/check-wire-removal.sh state
#   scripts/check-wire-removal.sh is_live v1.2.0    # explicit "oldest deployed" ref
#
# Exit 1 = a deployed reader subscripts it. Do NOT remove; deprecate instead.
set -uo pipefail

FIELD="${1:-}"
SINCE="${2:-}"
BRIDGE="integrations/claude-code/src/engram_mcp"

if [[ -z "$FIELD" ]]; then
    echo "usage: $0 <field-name> [git-ref of the oldest bridge still running]" >&2
    echo "  ref defaults to the last commit that touched the bridge." >&2
    exit 2
fi

cd "$(dirname "$0")/.." || exit 2

# Default: the last commit that changed the bridge. Anything older than that is
# what long-running sessions are still executing.
if [[ -z "$SINCE" ]]; then
    SINCE="$(git log -1 --format=%H -- "$BRIDGE")"
fi

echo "field:  ${FIELD}"
echo "ref:    ${SINCE:0:12}  (bridge as deployed to already-running sessions)"
echo

status=0

# A DIRECT SUBSCRIPT is the dangerous form — it raises KeyError when the field
# disappears. `.get()` degrades to None, which is a behaviour change to reason
# about but not a crash, so the two are reported separately rather than lumped.
scan() {
    local ref="$1" label="$2"
    local hits
    hits="$(git grep -n -E "\[[\"']${FIELD}[\"']\]" "$ref" -- "$BRIDGE" 2>/dev/null || true)"
    if [[ -n "$hits" ]]; then
        echo "⛔ ${label}: DIRECT SUBSCRIPT — removing this field raises KeyError"
        echo "$hits" | sed 's/^/     /'
        status=1
    else
        echo "✓  ${label}: no direct subscript"
    fi
    local soft
    soft="$(git grep -n -E "\.get\([\"']${FIELD}[\"']" "$ref" -- "$BRIDGE" 2>/dev/null || true)"
    if [[ -n "$soft" ]]; then
        echo "⚠️  ${label}: .get() reader(s) — no crash, but the value becomes None"
        echo "$soft" | sed 's/^/     /'
    fi
}

scan "$SINCE" "deployed bridge"
echo
scan "HEAD" "current bridge"

echo
if [[ $status -ne 0 ]]; then
    cat <<'EOF'
VERDICT: DO NOT REMOVE.
  Keep the field populated and mark it deprecated. It may only come off the wire
  once no bridge older than the change is running ANYWHERE on the fleet — which
  is a fact about deployed processes, not about whether a maintainer approved it.
  Ask the orchestrator for the deployed-reader list; it spawned them, so it is
  the only party that can enumerate them (engram cannot — its presence rows are
  keyed on the address, so they cannot say when a session started).
EOF
else
    cat <<'EOF'
VERDICT: no deployed reader subscripts this field.
  Still confirm nothing OUTSIDE this repo reads it before removing — this script
  only sees the bundled bridge.
EOF
fi
exit $status
