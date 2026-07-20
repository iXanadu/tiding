#!/usr/bin/env bash
# repo-hygiene-check.sh — grep tracked files for never-in-git patterns.
#
# Doctrine: assume any repo might slip public (docs/backlog-standard.md).
# This catches the mechanical residue: key material, private/overlay IPs,
# phone numbers, personal emails, plus an optional per-box denylist of
# names that must never appear (people, clients, internal hosts).
#
# Usage:
#   scripts/repo-hygiene-check.sh [repo-root]        # exit 1 on findings
#
# Denylist (optional, NEVER tracked — add to .gitignore):
#   <repo>/.hygiene-denylist  OR  ~/.config/repo-hygiene/denylist
#   One case-insensitive extended-regex pattern per line; '#' comments.
set -euo pipefail

ROOT="${1:-.}"
cd "$ROOT"
command -v git >/dev/null || { echo "not a git checkout"; exit 2; }
git rev-parse --git-dir >/dev/null 2>&1 || { echo "not a git checkout"; exit 2; }

fail=0
# Lines matching this are placeholders, not leaks: localhost targets, doc
# example domains (RFC 2606), format-string/template braces, <angle> stubs.
ALLOW='placeholder|@(localhost|127\.0\.0\.1)|example\.(com|org|net)|\{[A-Za-z_. ]+\}|<[^>]*>'
scan() { # scan <label> <extended-regex> [extra grep args...]
  local label="$1" pattern="$2"; shift 2
  local hits n
  hits=$(git ls-files -z \
    | xargs -0 grep -InE "$@" -- "$pattern" 2>/dev/null \
    | grep -vE '(^|/)repo-hygiene-check\.sh:' \
    | grep -vE "$ALLOW" || true)
  if [ -n "$hits" ]; then
    fail=1
    echo "✗ $label"
    echo "$hits" | head -20 | sed 's/^/    /'
    n=$(echo "$hits" | wc -l | tr -d ' ')
    if [ "$n" -gt 20 ]; then echo "    ... and $((n - 20)) more"; fi
  fi
}

# --- key material / credentials -------------------------------------------
scan "private key material"        '-----BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY'
scan "credential-looking token"    '\b(engram|ghp|gho|github_pat|sk-(live|test|proj|ant))_[A-Za-z0-9_-]{16,}'
scan "AWS access key id"           '\bAKIA[0-9A-Z]{16}\b'
scan "bearer/credential in URL"    '[a-z]+://[^ /@"]+:[^ /@"]{6,}@'
scan "inline password assignment"  '\b(password|passwd|secret)[ ]*[:=][ ]*["'\''][^"'\'' ]{6,}' -i

# --- network topology -------------------------------------------------------
# Private/CGNAT/overlay ranges. 127.x, 0.0.0.0, and doc-standard 192.0.2.x /
# 198.51.100.x / 203.0.113.x (RFC 5737) are allowed — use those in examples.
scan "private/LAN address (192.168/10.x/172.16-31)" \
  '\b(192\.168\.[0-9]{1,3}\.[0-9]{1,3}|10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3})\b'
scan "CGNAT/tailnet address (100.64-127.x)" \
  '\b100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}\b'
scan "tailscale MagicDNS hostname"  '\b[a-z0-9-]+\.tail[0-9a-f]{4,}\.ts\.net\b'

# --- PII ---------------------------------------------------------------------
scan "US-format phone number" \
  '(\+1[-. ]?)?\(?[2-9][0-9]{2}\)?[-. ][0-9]{3}[-. ][0-9]{4}\b'
# Emails: flag real-looking ones; allow example/test/noreply domains.
scan "personal email address" \
  '\b[A-Za-z0-9._%+-]+@(?!example\.|test\.|localhost)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b' -P

# --- local denylist ----------------------------------------------------------
for DL in ".hygiene-denylist" "$HOME/.config/repo-hygiene/denylist"; do
  if [ -f "$DL" ]; then
    while IFS= read -r pat; do
      case "$pat" in ''|\#*) continue;; esac
      scan "denylist: $pat" "$pat" -i
    done < "$DL"
  fi
done

if [ "$fail" -eq 0 ]; then
  echo "✓ repo-hygiene: clean"
else
  echo
  echo "Findings above violate the assume-public doctrine (docs/backlog-standard.md)."
  echo "Move the content to memory, genericize the example, or (rarely) add a"
  echo "deliberate exception comment and adjust the pattern."
fi
exit "$fail"
