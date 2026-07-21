#!/bin/bash
# Build the dashboard's static assets locally (no CDN at runtime).
#
# The dashboard/bridge pages previously loaded cdn.tailwindcss.com (runtime
# JIT) and an unpinned alpinejs from jsdelivr, with no SRI — a compromised
# CDN meant arbitrary script with the admin token in reach (2026-07-21
# audit). This script produces pinned, committed, locally-served assets:
#
#   server/static/alpine-<ver>.min.js   — pinned Alpine, SHA-256 verified
#   server/static/dashboard.css         — compiled Tailwind (dashboard theme)
#   server/static/bridge.css            — compiled Tailwind (bridge theme)
#
# Re-run only when template classes/themes change or to bump versions;
# commit the results. Requires node (npx). Network needed only to refresh.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(dirname "$SCRIPT_DIR")"
STATIC="$APP_DIR/server/static"
TEMPLATES="$APP_DIR/server/templates"

ALPINE_VERSION="3.14.9"
ALPINE_SHA256="3ed1eed252488921df65e363d6715deb04d7f92aaedb9e52199fdf73cb1e0ad3"
TAILWIND_VERSION="3.4.17"

mkdir -p "$STATIC"

# --- Alpine (pinned + integrity-checked) ---
ALPINE_OUT="$STATIC/alpine-${ALPINE_VERSION}.min.js"
if [ ! -f "$ALPINE_OUT" ]; then
    echo "Fetching alpinejs@${ALPINE_VERSION}..."
    curl -sL "https://cdn.jsdelivr.net/npm/alpinejs@${ALPINE_VERSION}/dist/cdn.min.js" -o "$ALPINE_OUT"
fi
echo "${ALPINE_SHA256}  ${ALPINE_OUT}" | shasum -a 256 -c - || {
    echo "ERROR: Alpine integrity check FAILED — refusing to keep the file."
    rm -f "$ALPINE_OUT"
    exit 1
}

# --- Tailwind (compiled per template — the two pages use different themes) ---
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cat > "$WORK/input.css" <<'CSS'
@tailwind base;
@tailwind components;
@tailwind utilities;
CSS

cat > "$WORK/dashboard.config.js" <<CONF
module.exports = {
  darkMode: 'class',
  content: ['$TEMPLATES/dashboard.html'],
  theme: { extend: { colors: { engram: {
    dark: '#0f172a', darker: '#020617', accent: '#a78bfa',
    accent2: '#34d399', surface: '#1e293b', border: '#334155',
  }}}},
}
CONF

cat > "$WORK/bridge.config.js" <<CONF
module.exports = {
  content: ['$TEMPLATES/bridge.html'],
  theme: { extend: { colors: { engram: {
    darker: '#0a0e17', dark: '#111827', surface: '#1a2332',
    border: '#2a3a4e', accent: '#60a5fa',
  }}}},
}
CONF

echo "Building dashboard.css..."
npx --yes "tailwindcss@${TAILWIND_VERSION}" -c "$WORK/dashboard.config.js" \
    -i "$WORK/input.css" -o "$STATIC/dashboard.css" --minify
echo "Building bridge.css..."
npx --yes "tailwindcss@${TAILWIND_VERSION}" -c "$WORK/bridge.config.js" \
    -i "$WORK/input.css" -o "$STATIC/bridge.css" --minify

echo ""
echo "=== Done ==="
ls -la "$STATIC"
echo "Commit the results — the server serves these from /static with no CDN."
