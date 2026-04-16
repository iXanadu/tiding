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

# --- Locate pyenv (same logic as install.sh) ---

PYENV_ROOT="${PYENV_ROOT:-$HOME/.pyenv}"
if [ ! -d "$PYENV_ROOT" ]; then
    echo "ERROR: pyenv root not found at $PYENV_ROOT"
    echo "Set PYENV_ROOT or install pyenv: https://github.com/pyenv/pyenv#installation"
    exit 1
fi

PYENV_BIN="$(command -v pyenv 2>/dev/null || echo "$PYENV_ROOT/bin/pyenv")"
if [ ! -x "$PYENV_BIN" ]; then
    for candidate in "$PYENV_ROOT/bin/pyenv" /opt/homebrew/bin/pyenv /usr/local/bin/pyenv; do
        if [ -x "$candidate" ]; then
            PYENV_BIN="$candidate"
            break
        fi
    done
fi

if [ ! -x "$PYENV_BIN" ]; then
    echo "ERROR: Cannot find pyenv binary"
    exit 1
fi

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

VENV_PIP="$PYENV_ROOT/versions/$VENV_NAME/bin/pip"
if [ ! -x "$VENV_PIP" ]; then
    echo "ERROR: pip not found at $VENV_PIP"
    exit 1
fi

echo "Installing engram-mcp (editable) into $VENV_NAME..."
"$VENV_PIP" install -e "$MCP_PKG" --quiet

# --- Verify ---

VENV_PY="$PYENV_ROOT/versions/$VENV_NAME/bin/python"
if "$VENV_PY" -c "from engram_mcp.scoping import resolve_project_name" 2>/dev/null; then
    echo "Verify: engram_mcp imports cleanly"
else
    echo "WARNING: engram_mcp import failed — install may be broken"
    exit 1
fi

echo ""
echo "=== Done ==="
echo "New Claude Code sessions will pick up the updated wrapper automatically"
echo "(the MCP bridge is spawned per-session; no restart needed)."
