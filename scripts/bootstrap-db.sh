#!/usr/bin/env bash
# bootstrap-db.sh — stand up PostgreSQL + pgvector for engram, no Docker needed.
#
# macOS (Homebrew): fully automated — installs postgresql@17 + pgvector,
# starts the service, creates the database. Idempotent; run it again freely.
# Linux: prints the exact package commands for your distro (varies too much
# to guess safely), then does the createdb/verify steps if PG is reachable.
#
# After this: ./scripts/install.sh   (or just: uvicorn server.main:app --port 8920)
set -euo pipefail

DB_NAME="${ENGRAM_DB_NAME:-engram}"

say() { printf '\n=== %s ===\n' "$*"; }

verify() {
  say "Verifying"
  if ! command -v psql >/dev/null; then
    echo "✗ psql not on PATH yet — open a new shell (or add your PG bin dir to PATH) and re-run."
    exit 1
  fi
  if ! psql -d "$DB_NAME" -c "SELECT 1" >/dev/null 2>&1; then
    echo "✗ Cannot connect to database '$DB_NAME'. Is PostgreSQL running?"
    exit 1
  fi
  # pgvector + pg_trgm are 'trusted' extensions: the DB owner may create them,
  # and the engram server does so automatically at startup. Prove it works:
  psql -d "$DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_trgm;" >/dev/null
  echo "✓ database '$DB_NAME' ready — pgvector + pg_trgm available"
  echo
  echo "Next:"
  echo "  ./scripts/install.sh        # run engram as a boot service"
  echo "  # or, just try it:"
  echo "  uvicorn server.main:app --port 8920"
  echo
  echo "Local peer auth? Put these in .env:  ENGRAM_DB_USER=$(whoami)  ENGRAM_DB_PASSWORD="
}

case "$(uname -s)" in
  Darwin)
    command -v brew >/dev/null || { echo "Homebrew required: https://brew.sh"; exit 1; }
    say "Installing PostgreSQL 17 + pgvector (Homebrew)"
    brew list postgresql@17 >/dev/null 2>&1 || brew install postgresql@17
    brew list pgvector      >/dev/null 2>&1 || brew install pgvector
    say "Starting PostgreSQL service"
    brew services start postgresql@17 >/dev/null || true
    # brew's keg-only PG needs its bin dir for psql/createdb in THIS shell:
    PGBIN="$(brew --prefix postgresql@17)/bin"
    export PATH="$PGBIN:$PATH"
    say "Waiting for PostgreSQL to accept connections"
    for _ in $(seq 1 30); do
      "$PGBIN/pg_isready" -q 2>/dev/null && break
      sleep 1
    done
    say "Creating database '$DB_NAME'"
    "$PGBIN/createdb" "$DB_NAME" 2>/dev/null || echo "(database already exists — fine)"
    verify
    ;;
  Linux)
    say "Linux: install PostgreSQL 17 + pgvector with your package manager"
    cat <<'EOF'
  # Debian/Ubuntu (PGDG repo — https://wiki.postgresql.org/wiki/Apt):
  sudo apt install postgresql-17 postgresql-17-pgvector

  # Fedora/RHEL:
  sudo dnf install postgresql17-server pgvector_17

  # Arch:
  sudo pacman -S postgresql pgvector

Then re-run this script to create + verify the database.
EOF
    if command -v pg_isready >/dev/null && pg_isready -q 2>/dev/null; then
      say "PostgreSQL detected running — creating database '$DB_NAME'"
      createdb "$DB_NAME" 2>/dev/null || echo "(database already exists — fine)"
      verify
    else
      echo "(PostgreSQL not running yet — install/start it, then re-run.)"
    fi
    ;;
  *)
    echo "Unsupported OS: $(uname -s). Install PostgreSQL 17 + pgvector manually, then: createdb $DB_NAME"
    exit 1
    ;;
esac
