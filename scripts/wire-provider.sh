#!/usr/bin/env bash
# wire-provider.sh — wire ONE agent provider (claude, grok, gpt, any HTTP
# harness) to engram on this box, per docs/design/provider-credentials.md.
#
# What it does:
#   1. Ensures ~/.config/engram/identities/<name> exists (0600) with a token:
#      - reuses an existing identity file, or
#      - mints a principal server-side when --admin-token is supplied, or
#      - accepts a raw token via --token.
#   2. Verifies the token end-to-end against the server (/whoami).
#   3. Prints the EXACT registration snippet for the provider's config —
#      the only line a provider config should carry is the selector.
#
# It deliberately does NOT edit provider config files (~/.claude.json,
# ~/.grok/config.toml, …) — those belong to their harnesses; you paste the
# snippet. It also never touches namespaces: the token decides those.
#
# Usage:
#   scripts/wire-provider.sh <name> [--kind claude|grok|http]
#                             [--server URL] [--token engram_...]
#                             [--admin-token engram_...] [--read NS,NS] [--write NS]
#
# Examples:
#   # mint + wire a new gpt identity (admin token from your secret store):
#   scripts/wire-provider.sh gpt --kind http --admin-token "$ADMIN_TOKEN"
#   # wire an existing token:
#   scripts/wire-provider.sh grok --kind grok --token engram_xxx
set -euo pipefail

NAME="${1:-}"; shift || true
[ -n "$NAME" ] || { echo "usage: wire-provider.sh <name> [--kind claude|grok|http] ..."; exit 2; }

KIND="http"; SERVER="http://localhost:8920"; TOKEN=""; ADMIN_TOKEN=""
READ_NS="fleet"; WRITE_NS="fleet"
while [ $# -gt 0 ]; do
  case "$1" in
    --kind) KIND="$2"; shift 2;;
    --server) SERVER="$2"; shift 2;;
    --token) TOKEN="$2"; shift 2;;
    --admin-token) ADMIN_TOKEN="$2"; shift 2;;
    --read) READ_NS="$2"; shift 2;;
    --write) WRITE_NS="$2"; shift 2;;
    *) echo "unknown arg: $1"; exit 2;;
  esac
done

CFG_DIR="$HOME/.config/engram"
ID_DIR="$CFG_DIR/identities"
ID_FILE="$ID_DIR/$NAME"
mkdir -p "$ID_DIR"; chmod 700 "$CFG_DIR" "$ID_DIR"

# --- 1. token ---------------------------------------------------------------
MINTED=0
if [ -f "$ID_FILE" ] && [ -z "$TOKEN" ]; then
  TOKEN=$(grep -E '^memory_api_token=' "$ID_FILE" | head -1 | cut -d= -f2 || true)
  [ -n "$TOKEN" ] && echo "• Reusing existing identity file: $ID_FILE"
fi
if [ -z "$TOKEN" ] && [ -n "$ADMIN_TOKEN" ]; then
  echo "• Minting principal '$NAME' (read: $READ_NS · write: $WRITE_NS) ..."
  RESP=$(curl -sf -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
    -d "{\"name\":\"$NAME\",\"type\":\"agent\",
         \"read_namespaces\":[$(echo "$READ_NS" | sed 's/[^,]*/"&"/g')],
         \"write_namespaces\":[$(echo "$WRITE_NS" | sed 's/[^,]*/"&"/g')]}" \
    "$SERVER/admin/principals") || { echo "✗ principal mint failed"; exit 1; }
  TOKEN=$(printf '%s' "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('raw_token',''))")
  [ -n "$TOKEN" ] || { echo "✗ no token in mint response (principal may already exist — pass --token, or regenerate via POST /admin/principals/$NAME/token)"; exit 1; }
  MINTED=1
fi
[ -n "$TOKEN" ] || { echo "✗ No token: pass --token, --admin-token (to mint), or pre-create $ID_FILE"; exit 1; }

# --- 2. identity file -------------------------------------------------------
# Write when the file is absent OR we just minted a token (a raw token is shown
# only once — never discard it). Always tighten perms, even on a reused file.
if [ ! -f "$ID_FILE" ] || [ "$MINTED" = "1" ]; then
  { echo "memory_api_token=$TOKEN"; echo "memory_api_url=$SERVER"; } > "$ID_FILE"
  echo "• Wrote $ID_FILE"
fi
chmod 600 "$ID_FILE"

# --- 3. verify ---------------------------------------------------------------
WHO=$(curl -sf -H "Authorization: Bearer $TOKEN" "$SERVER/whoami") \
  || { echo "✗ /whoami failed — token invalid or server unreachable at $SERVER"; exit 1; }
PRINCIPAL=$(printf '%s' "$WHO" | python3 -c "import sys,json; print(json.load(sys.stdin).get('name',''))")
echo "✓ Verified: token authenticates as principal '$PRINCIPAL' at $SERVER"
[ "$PRINCIPAL" = "$NAME" ] || echo "  ⚠ principal name ($PRINCIPAL) differs from identity name ($NAME) — allowed, but make sure it's intentional"

# --- 4. registration snippet -------------------------------------------------
echo
echo "── Paste into the provider's config ─────────────────────────────────"
case "$KIND" in
  claude)
    cat <<EOF
# ~/.claude.json → mcpServers (env block stays EMPTY if this box's default
# identity IS '$NAME' via the legacy ~/.config/engram/identity symlink;
# otherwise add the selector):
"claude-memory": {
  "type": "stdio",
  "command": "<pyenv>/versions/cc-memory-3.12/bin/python",
  "args": ["-m", "engram_mcp.server"],
  "env": { "ENGRAM_IDENTITY": "$NAME" }
}
EOF
    ;;
  grok)
    cat <<EOF
# ~/.grok/config.toml
[mcp_servers.engram]
command = "<pyenv>/versions/cc-memory-3.12/bin/python"
args = ["-m", "engram_mcp.server"]
enabled = true

[mcp_servers.engram.env]
ENGRAM_IDENTITY = "$NAME"
EOF
    ;;
  http|*)
    cat <<EOF
# Raw-HTTP harness: read the token from the identity file at call time —
#   token: \$(grep '^memory_api_token=' $ID_FILE | cut -d= -f2)
#   base:  $SERVER   (Bearer auth on every POST)
# Or, if the harness runs the engram MCP bridge, set in its env:
ENGRAM_IDENTITY=$NAME
# Optional (coalition channels, launcher-injected): ENGRAM_CHANNELS="#chan1,#chan2"
EOF
    ;;
esac
echo "──────────────────────────────────────────────────────────────────────"
echo "Rules: the selector is the ONLY engram line a provider config carries."
echo "Never set memory_namespace / memory_read_namespaces — the token decides."