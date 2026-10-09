#!/usr/bin/env bash
# PUBLIC-SURFACE-2 end-to-end proof: an ADMIN token is refused on the public
# edge and still works locally; a scoped token works on both.
#
# Reads tokens BY LABEL from ~/.config/engram.keys (format: `label = value`).
# Never echoes a token. Run on hosta:  scripts/verify-public-surface.sh
set -u
KEYS="${ENGRAM_KEYS_FILE:-$HOME/.config/engram.keys}"
PUBLIC="${ENGRAM_PUBLIC_URL:-https://engram.example.com}"
LOCAL="${ENGRAM_LOCAL_URL:-http://localhost:8920}"
BODY='{"namespace":"fleet","key":"verify-public-surface-probe"}'

tok() {  # tok <label>  -> value, from the keys file, never printed
  sed -nE "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$KEYS" | head -1 | tr -d '[:space:]'
}
call() {  # call <token> <base-url> -> http status
  curl -s -m 20 -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer $1" -H 'Content-Type: application/json' \
    -d "$BODY" "$2/memory/get"
}
check() {  # check <label> <where> <status> <expected>
  if [ "$3" = "$4" ]; then printf '  PASS  %-12s %-7s %s\n' "$1" "$2" "$3"
  else printf '  FAIL  %-12s %-7s got %s, expected %s\n' "$1" "$2" "$3" "$4"; RC=1; fi
}

[ -r "$KEYS" ] || { echo "cannot read $KEYS"; exit 2; }
RC=0
for label in owner claude-code; do
  t="$(tok "$label")"
  [ -n "$t" ] || { echo "  SKIP  $label: no such label in $KEYS"; RC=1; continue; }
  case "$label" in
    owner)     exp_pub=403; exp_loc=200 ;;   # admin: refused on the edge, fine at home
    claude-code) exp_pub=200; exp_loc=200 ;;   # scoped: unaffected everywhere
  esac
  check "$label" public "$(call "$t" "$PUBLIC")" "$exp_pub"
  check "$label" local  "$(call "$t" "$LOCAL")"  "$exp_loc"
done
# WEBPUSH-1: the mail signal is the one login-free route under /memory/. A
# junk key must look exactly like a missing route, and its neighbours must
# still demand a token.
anon() {  # anon <method> <path> -> http status, no credential
  curl -s -m 20 -o /dev/null -w '%{http_code}' -X "$1" "$PUBLIC$2"
}
check anon-signal public "$(anon GET /memory/signal/sig_verify_junk)" 404
check anon-bare   public "$(anon GET /memory/signal)" 401
check anon-search public "$(anon POST /memory/search)" 401
echo
[ "$RC" = 0 ] && echo "ALL PASS — admin refused on the public edge, scoped principals unaffected." \
             || echo "SOMETHING FAILED — paste this output back to engram."
exit "$RC"
