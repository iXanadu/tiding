#!/bin/bash
# Install (or update) the Claude Code MCP wrapper (engram-mcp) into cc-memory-3.12.
#
# This is the lightweight update script — run it on any host where the engram
# MCP bridge is installed but the server is not. Safe to re-run; it's an
# editable install, so `git pull && ./scripts/install-mcp-wrapper.sh` is all
# you need after updating integrations/claude-code/.
#
# Usage (run from anywhere — script resolves its own location):
#   /path/to/engram/scripts/install-mcp-wrapper.sh
#
# Or from the repo:
#   ./scripts/install-mcp-wrapper.sh

set -e

# Resolve the script's own directory, regardless of caller CWD
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(dirname "$SCRIPT_DIR")"
MCP_PKG="$APP_DIR/integrations/claude-code"
VENV_NAME="cc-memory-3.12"
PYTHON_VERSION="3.12"

echo "=== engram MCP wrapper install ==="
echo "Repo:   $APP_DIR"
echo "Target: $MCP_PKG"

# Sanity check — we must be inside an engram checkout
if [ ! -d "$MCP_PKG" ] || [ ! -f "$MCP_PKG/pyproject.toml" ]; then
    echo "ERROR: $MCP_PKG not found (or missing pyproject.toml)"
    echo "This script must live in <engram-repo>/scripts/ — did you move it?"
    exit 1
fi

# --- Locate pyenv (shared resolver — knows every fleet root, not just ~/.pyenv) ---

RESOLVE="$SCRIPT_DIR/resolve-venv-python.sh"
PYENV_ROOT="$("$RESOLVE" --root)" || {
    echo "Install pyenv: https://github.com/pyenv/pyenv#installation"
    exit 1
}
# Export so `pyenv virtualenv` creates under THIS root — without it, a bare
# shell on a /usr/local/pyenv box would silently default to ~/.pyenv.
export PYENV_ROOT
PYENV_BIN="$("$RESOLVE" --pyenv-bin)" || exit 1

echo "pyenv:  $PYENV_BIN"

# --- Ensure cc-memory-3.12 virtualenv exists ---

if "$PYENV_BIN" versions --bare 2>/dev/null | grep -q "^${VENV_NAME}$"; then
    echo "venv:   $VENV_NAME (existing)"
else
    PY_FULL=$("$PYENV_BIN" versions --bare 2>/dev/null | grep "^${PYTHON_VERSION}\." | grep -v '/' | sort -V | tail -1)
    if [ -z "$PY_FULL" ]; then
        echo "ERROR: No Python ${PYTHON_VERSION}.x installed in pyenv"
        echo "Run: pyenv install ${PYTHON_VERSION}"
        exit 1
    fi
    echo "venv:   creating $VENV_NAME from Python $PY_FULL..."
    "$PYENV_BIN" virtualenv "$PY_FULL" "$VENV_NAME"
fi

# --- Install (editable) ---

# Resolve through the shared resolver: the venv may live under a different
# pyenv root than the first-found one (e.g. /usr/local/pyenv on Linux).
VENV_PIP="$("$RESOLVE" "$VENV_NAME" pip)" || exit 1

echo "Installing engram-mcp (editable) into $VENV_NAME..."
"$VENV_PIP" install -e "$MCP_PKG" --quiet

# --- Verify ---

VENV_PY="$("$RESOLVE" "$VENV_NAME" python)" || exit 1
if "$VENV_PY" -c "from engram_mcp.scoping import resolve_project_name" 2>/dev/null; then
    echo "Verify: engram_mcp imports cleanly"
else
    echo "WARNING: engram_mcp import failed — install may be broken"
    exit 1
fi

# --- Stable command paths (fleet-uniform) ---
# Symlink the console scripts to /usr/local/bin so .claude.json, launchd,
# systemd, and skill docs reference ONE path on every box, regardless of
# where pyenv lives (~/.pyenv on macOS, /usr/local/pyenv on shared Linux).
# Best-effort: a failed symlink degrades to the venv path, never the install.
STABLE_BIN_DIR="/usr/local/bin"
VENV_BIN_DIR="$(dirname "$VENV_PY")"
for cmd in engram-mcp engram-inbox-wait engram-doctor; do
    src="$VENV_BIN_DIR/$cmd"
    if [ ! -x "$src" ]; then
        echo "WARNING: $cmd not found in venv — skipping symlink"
        continue
    fi
    if [ -w "$STABLE_BIN_DIR" ]; then
        ln -sf "$src" "$STABLE_BIN_DIR/$cmd" && echo "Symlink: $STABLE_BIN_DIR/$cmd -> $src"
    elif sudo -n true 2>/dev/null || [ -t 0 ]; then
        sudo ln -sf "$src" "$STABLE_BIN_DIR/$cmd" && echo "Symlink: $STABLE_BIN_DIR/$cmd -> $src" || \
            echo "WARNING: could not symlink $STABLE_BIN_DIR/$cmd (continuing — venv path still works)"
    else
        echo "WARNING: $STABLE_BIN_DIR not writable and no sudo — skipping $cmd symlink"
    fi
done

echo ""
echo "=== Done ==="
echo "Stable commands: /usr/local/bin/engram-{mcp,inbox-wait,doctor}"
echo "New Claude Code sessions will pick up the updated wrapper automatically"
echo "(the MCP bridge is spawned per-session; no restart needed)."
