#!/usr/bin/env bash
# ACCEPT-1: per-provider identity acceptance harness (assertion list v2,
# ratified 2026-08-13 — see project memory `backlog/ACCEPT-1`).
#
# Spawns a real server (scratch port, `engram_accept` DB) and real bridge
# sessions per provider; asserts the spawn-to-despawn lifecycle as WORLD
# outcomes. ~30s. Never touches prod state.
#
# Usage: scripts/accept.sh [extra pytest args]
set -euo pipefail
cd "$(dirname "$0")/.."
exec python -m pytest acceptance/ -q "$@"
