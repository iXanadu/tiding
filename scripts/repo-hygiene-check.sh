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

ALLOW_NO_DENYLIST=0
args=()
for a in "$@"; do
  case "$a" in
    --no-denylist) ALLOW_NO_DENYLIST=1 ;;
    *) args+=("$a") ;;
  esac
done
set -- "${args[@]+"${args[@]}"}"

ROOT="${1:-.}"
cd "$ROOT"
command -v git >/dev/null || { echo "not a git checkout"; exit 2; }
git rev-parse --git-dir >/dev/null 2>&1 || { echo "not a git checkout"; exit 2; }

fail=0
# Lines matching this are placeholders, not leaks: localhost targets, doc
# example domains (RFC 2606), format-string/template braces, <angle> stubs.
ALLOW='placeholder|@email\.com|postgresql@|@(localhost|127\.0\.0\.1|test\.)|example\.(com|org|net)|users\.noreply\.github\.com|\{[A-Za-z_. ]+\}|<[^>]*>'
# HYGIENE-UNTRACKED-1: what gets scanned, computed ONCE so every scan and the
# final verdict describe the same set.
#
# `git ls-files` alone is TRACKED FILES ONLY, and a new file is untracked until
# the instant it is committed — so the check was blind to exactly the files most
# likely to carry a fresh leak, and printed "clean (denylist enforced)" about
# content it had never opened. Measured 2026-08-29 by walking into it: the check
# passed, and the commit reached the public remote carrying a denylisted
# hostname 11 times across two brand-new test files.
#
# Untracked-but-not-ignored files are therefore included. Ignored files stay
# out on purpose: .env and friends are ignored BECAUSE they hold secrets, they
# are never pushed, and scanning them would fail every run for the one reason
# that does not matter.
FILES_TRACKED=$(git ls-files)
FILES_NEW=$(git ls-files -o --exclude-standard)
N_TRACKED=$(printf '%s' "$FILES_TRACKED" | grep -c . || true)
N_NEW=$(printf '%s' "$FILES_NEW" | grep -c . || true)
scan_files() { printf '%s\n%s\n' "$FILES_TRACKED" "$FILES_NEW" | grep -v '^$'; }

# HYGIENE-BASELINE-1: a denylist is box-wide and cannot know that a name sits
# in a file BECAUSE it belongs there — a copyright holder must be named in the
# NOTICE. Left unexcused, such a name fails the check forever, and a check that
# can never pass gets bypassed, which is worse than the name. Exceptions live
# in a TRACKED file so adding one is a visible, reviewable act (the denylists
# themselves are deliberately untracked). Each rule must name BOTH a file and a
# pattern: a bare pattern would be a hole, not an exception.
apply_allowfile() { # stdin: grep hits -> stdout: hits minus excused ones
  if [ ! -f .hygiene-allow ]; then cat; return; fi
  local out rule path pat
  out=$(cat)
  while IFS= read -r rule; do
    case "$rule" in ''|\#*) continue;; esac
    rule="${rule%%#*}"                       # strip trailing reason comment
    rule="$(printf '%s' "$rule" | sed -E 's/[[:space:]]+$//')"
    case "$rule" in *:*) ;; *) continue;; esac
    path="${rule%%:*}"; pat="${rule#*:}"
    [ -n "$path" ] && [ -n "$pat" ] || continue
    out=$(printf '%s' "$out" | grep -vE "^${path}[^:]*:[0-9]+:.*${pat}" || true)
  done < .hygiene-allow
  printf '%s' "$out"
}

scan() { # scan <label> <extended-regex> [extra grep args...]
  local label="$1" pattern="$2"; shift 2
  local hits n
  hits=$(scan_files | tr '\n' '\0' \
    | xargs -0 grep -InE "$@" -- "$pattern" 2>/dev/null \
    | grep -vE '(^|/)repo-hygiene-check\.sh:' \
    | grep -vE "$ALLOW" \
    | apply_allowfile || true)
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
# ERE, not -P: the system grep has no -P, and with stderr discarded this scan
# had been failing silently since it was written. Doc/test domains are
# excluded by ALLOW below instead of by a lookahead.
scan "personal email address" \
  '\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'

# --- local denylist ----------------------------------------------------------
# ⚠️ THIS BLOCK USED TO FAIL SILENTLY. If no denylist existed, the scan for
# people / clients / internal hostnames simply did not run, and the script
# still printed "✓ repo-hygiene: clean". Measured 2026-08-26: nine consecutive
# commits were reported clean by a check that had never once looked at a
# hostname. A check that skips is not a check that passes — say so, loudly.
denylist_found=0
for DL in ".hygiene-denylist" "$HOME/.config/repo-hygiene/denylist"; do
  if [ -f "$DL" ]; then
    denylist_found=1
    while IFS= read -r pat; do
      case "$pat" in ''|\#*) continue;; esac
      # A leading/trailing \b in the denylist means "the name as a whole
      # token". ERE's \b treats '_' as a word character, so NAME_ENGRAM_URL
      # and on_name slipped through the 2026-08-26 scrub AND this check.
      # Translate to alphanumeric-only boundaries. Plain ERE on purpose:
      # the grep xargs finds is the system one, which has no -P.
      pat="$(printf '%s' "$pat" | sed -E 's/^\\b/(^|[^A-Za-z0-9])/; s/\\b$/([^A-Za-z0-9]|$)/')"
      scan "denylist: $pat" "$pat" -i
    done < "$DL"
  fi
done

if [ "$denylist_found" -eq 0 ] && [ "$ALLOW_NO_DENYLIST" -eq 0 ]; then
  fail=1
  echo "✗ NO DENYLIST — the people / clients / internal-hosts scan DID NOT RUN"
  echo "    This is not a pass. Key material, private IPs, phone numbers and"
  echo "    emails were checked; NAMES were not looked at even once."
  echo "    Create one (never tracked; already in .gitignore):"
  echo "      .hygiene-denylist   or   \$HOME/.config/repo-hygiene/denylist"
  echo "    One case-insensitive extended-regex per line, '#' for comments."
  echo "    To deliberately run without one, pass --no-denylist."
  echo
fi

if [ "$fail" -eq 0 ]; then
  # HYGIENE-UNTRACKED-1: a clean result must be able to state its own SCOPE.
  # "clean" that cannot say what it read is how this check passed a commit
  # carrying a denylisted name — the miss was not the defect, the unqualified
  # verdict was.
  scope="${N_TRACKED} tracked"
  if [ "${N_NEW:-0}" -gt 0 ]; then scope="$scope + ${N_NEW} new"; fi
  scope="$scope files scanned"
  if [ "$denylist_found" -eq 1 ]; then
    echo "✓ repo-hygiene: clean (denylist enforced; $scope)"
  else
    # Say what was NOT checked. "clean" with a skipped scan behind it is the
    # exact thing this script was changed to stop doing.
    echo "✓ repo-hygiene: clean — BUT NO DENYLIST: names were not checked ($scope)"
  fi
else
  echo
  echo "Findings above violate the assume-public doctrine (docs/backlog-standard.md)."
  echo "Move the content to memory, genericize the example, or (rarely) add a"
  echo "deliberate exception comment and adjust the pattern."
fi
exit "$fail"
