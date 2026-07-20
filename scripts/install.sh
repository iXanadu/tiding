#!/bin/bash
# Install engram as a launchd service (macOS) or systemd service (Linux)
#
# Prerequisites:
#   - pyenv + pyenv-virtualenv installed
#   - Python 3.12+ available via pyenv
#   - PostgreSQL 17+ with pgvector extension
#   (embeddings run in-process via sentence-transformers; the model
#    downloads from HuggingFace on first run — no external service needed)
#
# Usage:
#   cd /opt/srv/engram   # or wherever you cloned
#   ./scripts/install.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(dirname "$SCRIPT_DIR")"
VENV_NAME="engram-3.12"
PYTHON_VERSION="3.12"
LABEL="com.engram"

echo "=== engram Install ==="
echo "App directory: $APP_DIR"

# --- Python environment ---

# Find pyenv binary (works for both Homebrew and git installs)
PYENV_ROOT="${PYENV_ROOT:-$HOME/.pyenv}"
if [ ! -d "$PYENV_ROOT" ]; then
    echo "ERROR: pyenv root not found at $PYENV_ROOT"
    echo "Install pyenv: https://github.com/pyenv/pyenv#installation"
    exit 1
fi

# Locate the pyenv binary — Homebrew puts it in /opt/homebrew/bin, git install in $PYENV_ROOT/bin
PYENV_BIN="$(command -v pyenv 2>/dev/null || echo "$PYENV_ROOT/bin/pyenv")"
if [ ! -x "$PYENV_BIN" ]; then
    # command -v returns the shell function name, not a path — fall back to known locations
    for candidate in "$PYENV_ROOT/bin/pyenv" /opt/homebrew/bin/pyenv /usr/local/bin/pyenv; do
        if [ -x "$candidate" ]; then
            PYENV_BIN="$candidate"
            break
        fi
    done
fi

if [ ! -x "$PYENV_BIN" ]; then
    echo "ERROR: Cannot find pyenv binary"
    echo "Install pyenv: https://github.com/pyenv/pyenv#installation"
    exit 1
fi

echo "Using pyenv: $PYENV_BIN"

# Check if the virtualenv already exists
if "$PYENV_BIN" versions --bare 2>/dev/null | grep -q "^${VENV_NAME}$"; then
    echo "pyenv virtualenv '$VENV_NAME' already exists"
else
    # Find an installed 3.12.x version
    PY_FULL=$("$PYENV_BIN" versions --bare 2>/dev/null | grep "^${PYTHON_VERSION}\." | sort -V | tail -1)
    if [ -z "$PY_FULL" ]; then
        echo "ERROR: No Python ${PYTHON_VERSION}.x installed in pyenv"
        echo "Run: pyenv install ${PYTHON_VERSION}"
        exit 1
    fi
    echo "Creating virtualenv '$VENV_NAME' from Python $PY_FULL..."
    "$PYENV_BIN" virtualenv "$PY_FULL" "$VENV_NAME"
fi

# Set local .python-version
echo "$VENV_NAME" > "$APP_DIR/.python-version"

# Install dependencies
VENV_PIP="$PYENV_ROOT/versions/$VENV_NAME/bin/pip"
echo "Installing dependencies..."
"$VENV_PIP" install -e "$APP_DIR" --quiet

# --- .env ---

if [ ! -f "$APP_DIR/.env" ]; then
    if [ -f "$APP_DIR/.env.example" ]; then
        cp "$APP_DIR/.env.example" "$APP_DIR/.env"
        echo "Created .env from .env.example — edit it with your DB credentials"
    else
        echo "WARNING: No .env.example found. Create .env manually."
    fi
else
    echo ".env already exists"
fi

# --- Logs directory ---

mkdir -p "$APP_DIR/logs"

# NOTE: engram's installer deliberately does NOT touch ~/.claude/CLAUDE.md or
# any other provider-global agent config. Wiring engram into an agent's global
# instructions is the operator's call — see docs/getting-started.md and
# docs/design/provider-credentials.md for what to add.

# --- Claude Code MCP sub-package (engram-mcp) ---

MCP_VENV="cc-memory-3.12"
MCP_PKG="$APP_DIR/integrations/claude-code"

if [ -d "$MCP_PKG" ]; then
    MCP_PIP="$PYENV_ROOT/versions/$MCP_VENV/bin/pip"
    if [ -x "$MCP_PIP" ]; then
        echo "Installing engram-mcp into $MCP_VENV..."
        "$MCP_PIP" install -e "$MCP_PKG" --quiet
        echo "engram-mcp installed"
    else
        echo "WARNING: $MCP_VENV virtualenv not found — skipping engram-mcp install"
        echo "Create it: pyenv virtualenv 3.12 $MCP_VENV"
    fi
else
    echo "NOTE: No integrations/claude-code/ found — skipping MCP install"
fi

# --- Service installation ---

UVICORN="$PYENV_ROOT/versions/$VENV_NAME/bin/uvicorn"

if [[ "$(uname)" == "Darwin" ]]; then
    # macOS: LaunchDaemon (starts at boot, no login required)
    PLIST_PATH="/Library/LaunchDaemons/${LABEL}.plist"
    RUN_USER="$(whoami)"

    cat > /tmp/${LABEL}.plist <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>UserName</key>
    <string>${RUN_USER}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${UVICORN}</string>
        <string>server.main:app</string>
        <string>--host</string>
        <string>0.0.0.0</string>
        <string>--port</string>
        <string>8920</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${APP_DIR}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>${HOME}</string>
        <key>PATH</key>
        <string>/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <!-- Embed model is pinned + fully cached; skip the online HF metadata
             revalidation at boot. Avoids a startup race (httpx client closed
             mid-load) that crash-loops the service. Flip OFF only for a
             deliberate embed-model change that needs a re-download. -->
        <key>HF_HUB_OFFLINE</key>
        <string>1</string>
        <key>TRANSFORMERS_OFFLINE</key>
        <string>1</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${APP_DIR}/logs/engram.log</string>
    <key>StandardErrorPath</key>
    <string>${APP_DIR}/logs/engram.err</string>
</dict>
</plist>
PLIST

    sudo cp /tmp/${LABEL}.plist "$PLIST_PATH"
    sudo chown root:wheel "$PLIST_PATH"
    sudo chmod 644 "$PLIST_PATH"
    rm /tmp/${LABEL}.plist

    echo "Installed LaunchDaemon: $PLIST_PATH"
    echo ""
    echo "=== Install Complete ==="
    echo ""
    echo "Next steps:"
    echo "  1. Edit .env with your database credentials"
    echo "  2. Start: ./scripts/start.sh"
    echo "  3. Verify: curl http://localhost:8920/health"

elif [[ "$(uname)" == "Linux" ]]; then
    # Linux: systemd
    SERVICE_PATH="/etc/systemd/system/engram.service"

    cat > /tmp/engram.service <<SERVICE
[Unit]
Description=engram FastAPI service
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=${APP_DIR}
ExecStart=${UVICORN} server.main:app --host 0.0.0.0 --port 8920
Restart=always
RestartSec=5
Environment=PATH=${PYENV_ROOT}/versions/${VENV_NAME}/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
# Embed model is pinned + fully cached; skip the online HF metadata
# revalidation at boot (avoids a startup race that crash-loops the service).
# Flip OFF only for a deliberate embed-model change needing a re-download.
Environment=HF_HUB_OFFLINE=1
Environment=TRANSFORMERS_OFFLINE=1

[Install]
WantedBy=multi-user.target
SERVICE

    echo "Systemd unit written to /tmp/engram.service"
    echo "Install with: sudo cp /tmp/engram.service $SERVICE_PATH && sudo systemctl daemon-reload"
    echo ""
    echo "=== Install Complete ==="
    echo ""
    echo "Next steps:"
    echo "  1. Edit .env with your database credentials"
    echo "  2. sudo systemctl enable --now engram"
    echo "  3. Verify: curl http://localhost:8920/health"

else
    echo "ERROR: Unsupported platform: $(uname)"
    exit 1
fi
