#!/bin/bash
# Resolve pyenv paths uniformly, regardless of where pyenv lives on this box.
#
# Interpreter paths differ per machine (macOS convention: ~/.pyenv; shared
# Linux convention: /usr/local/pyenv). Every script that hardcodes one of
# those breaks on the other kind of box. This resolver is the single place
# that knows the search order — everything else asks it.
#
# Usage:
#   resolve-venv-python.sh <venv-name> [binary]   # abs path to a binary inside
#                                                 # the venv (default: python)
#   resolve-venv-python.sh --root                 # the pyenv root directory
#   resolve-venv-python.sh --pyenv-bin            # the pyenv executable
#
# Exit codes: 0 = printed a path; 1 = not found (message on stderr).
#
# Search order (first match wins):
#   $PYENV_ROOT (explicit override) > ~/.pyenv > /usr/local/pyenv > /opt/pyenv
# For venv lookups, a root only matches if it actually CONTAINS the venv —
# a box with both roots resolves to the one that has it.

set -u

_roots() {
    [ -n "${PYENV_ROOT:-}" ] && echo "$PYENV_ROOT"
    echo "$HOME/.pyenv"
    echo "/usr/local/pyenv"
    echo "/opt/pyenv"
}

# A directory only counts as a pyenv root if it looks like one: it holds a
# pyenv binary or a non-empty versions/. (Real case: a stale, half-removed
# ~/.pyenv shadowing the box's real /usr/local/pyenv — webone, 2026-07-21.)
_is_real_root() {
    [ -x "$1/bin/pyenv" ] && return 0
    [ -d "$1/versions" ] && [ -n "$(ls -A "$1/versions" 2>/dev/null)" ] && return 0
    return 1
}

resolve_root() {
    local root first_existing=""
    while IFS= read -r root; do
        [ -d "$root" ] || continue
        [ -n "$first_existing" ] || first_existing="$root"
        _is_real_root "$root" && { echo "$root"; return 0; }
    done < <(_roots)
    # Fallback: a fresh box may have an empty root before its first venv.
    [ -n "$first_existing" ] && { echo "$first_existing"; return 0; }
    echo "ERROR: no pyenv root found (searched: \$PYENV_ROOT, ~/.pyenv, /usr/local/pyenv, /opt/pyenv)" >&2
    return 1
}

resolve_pyenv_bin() {
    # `command -v` first (PATH-installed, e.g. Homebrew), then known locations.
    local bin root
    bin="$(command -v pyenv 2>/dev/null || true)"
    [ -n "$bin" ] && [ -x "$bin" ] && { echo "$bin"; return 0; }
    while IFS= read -r root; do
        [ -x "$root/bin/pyenv" ] && { echo "$root/bin/pyenv"; return 0; }
    done < <(_roots)
    for bin in /opt/homebrew/bin/pyenv /usr/local/bin/pyenv; do
        [ -x "$bin" ] && { echo "$bin"; return 0; }
    done
    echo "ERROR: pyenv binary not found" >&2
    return 1
}

resolve_venv_binary() {
    local venv="$1" binary="${2:-python}" root candidate
    while IFS= read -r root; do
        candidate="$root/versions/$venv/bin/$binary"
        [ -x "$candidate" ] && { echo "$candidate"; return 0; }
    done < <(_roots)
    echo "ERROR: '$binary' not found in venv '$venv' under any pyenv root" >&2
    echo "       (searched: \$PYENV_ROOT, ~/.pyenv, /usr/local/pyenv, /opt/pyenv)" >&2
    return 1
}

case "${1:-}" in
    --root)      resolve_root ;;
    --pyenv-bin) resolve_pyenv_bin ;;
    ""|--help|-h)
        grep '^# ' "$0" | sed 's/^# \{0,1\}//'
        [ "${1:-}" = "" ] && exit 1 || exit 0
        ;;
    *)           resolve_venv_binary "$1" "${2:-python}" ;;
esac
