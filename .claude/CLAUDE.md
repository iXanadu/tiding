# engram

Generic semantic memory service for AI agents. FastAPI + pgvector + Ollama embeddings.

## Project Structure

- `server/` — FastAPI app (config, db, embeddings, auth, routers, services)
  - `server/services/` — memory_service.py (all CRUD), principal_service.py (identity/auth)
  - `server/routers/` — memory.py, admin.py, principals.py
  - `server/auth.py` — PrincipalAuthMiddleware (two-mode: enrichment vs enforcement)
  - `server/dependencies.py` — auth helpers (get_current_principal, require_admin, check_namespace_access)
- `integrations/homeassistant/` — Pyscript client + Blueprint for HA voice assistants
- `integrations/claude-code/` — MCP bridge (engram-mcp) for Claude Code
- `scripts/` — install/start/restart/uninstall + ollama-warmup + migrate_ha_memory
- `launchd/` + `systemd/` — Service templates
- `tests/` — 88 tests (API, auth, admin, embeddings, e2e, memory_service, principal_service, principals_api, permissions, bootstrap)
- `docs/` — System prompts, model selection, project-migration guide, CLAUDE.md.global

## Commands

- Run server: `uvicorn server.main:app --host 0.0.0.0 --port 8920`
- Run server tests: `pytest tests/ -v`
- Run MCP tests: `cd integrations/claude-code && PYENV_VERSION=cc-memory-3.12 pytest tests/ -v`
- Health check: `curl http://localhost:8920/health`

## Conventions

- Config prefix: `ENGRAM_` (env vars and `.env` file)
- Database: `engram` (PostgreSQL + pgvector + pg_trgm)
- Default port: 8920
- Embedding model: nomic-embed-text via Ollama (:11434)
- pyenv virtualenv: `engram-3.12` (`.python-version` in repo)
- All memory CRUD goes through `server/services/memory_service.py`
- Schema auto-created on startup; migration SQL handles upgrades from older schemas

## Data Model

Three independent dimensions scope every memory:

| Dimension | Purpose | Examples |
|-----------|---------|----------|
| **namespace** | Which system (required, no default) | `claude-code`, `ha`, `beast` |
| **scope** | Visibility level | `shared`, `machine`, `project`, `user` |
| **user_id** | Identity within namespace | `global`, hostname, dirname, HA UUID |

UNIQUE constraint: `(namespace, key, scope, user_id)`

## Principals (Auth System)

Identity and access control for the API. Two phases implemented:

- **Tables**: principals, principal_aliases, consent_grants, audit_log
- **Config**: `ENGRAM_REQUIRE_AUTH` (bool) — when true, enforces principal token auth on all requests
- **Bootstrap**: auto-creates `_bootstrap` admin when require_auth=true + no admins exist
- **Endpoints**: full CRUD under `/admin/principals` (9 endpoints)
- **Namespace enforcement**: read/write permission checks on memory and admin endpoints
- **Token format**: `engram_<random>` (bcrypt-hashed in DB, raw shown once at creation)

Auth modes:
- `require_auth=false` (default): legacy `ENGRAM_API_TOKEN` check, anonymous allowed if no token configured
- `require_auth=true`: Bearer token must match a principal in the principals table

## Critical Gotchas

- `namespace` is **required** on all API calls — no default. Omitting it returns 422.
- Pyscript `@service` decorators MUST use `supports_response="optional"` for HA 2024.10+.
- Dev (`~/projects/engram`) and prod (`/opt/srv/engram`) can both be `pip install -e` in the same pyenv virtualenv — last install wins for import resolution. After editing locally, run `pip install -e .` from dev dir.
- Project `user_id` for `scope=project` is resolved via `.engram.cfg` walk-up (see `integrations/claude-code/src/engram_mcp/scoping.py`). Basename is only a fallback — required because server layouts like `/var/www/site/prod` would otherwise collide. Every project must commit a `.engram.cfg` at the repo root.

## Related Projects

- `integrations/claude-code/` — MCP bridge (engram-mcp) that CC uses to talk to engram. Installed in `cc-memory-3.12` pyenv. Replaces the old standalone `claude-memory-mcp` repo (archived).
- ha-semantic-memory — The original (deprecated, archived on GitHub 2026-02-13). Data migrated to engram DB.
- `docs/CLAUDE.md.global` — Global `~/.claude/CLAUDE.md` template for new machines. Copy to `~/.claude/CLAUDE.md` after cloning.

## State Management

**This project uses memory-first state tracking.** No `claude/CODEBASE_STATE.md` or `claude/session_progress/` files. All session state, decisions, and progress live in persistent memory at `scope=project`. Search memory on startup, store at milestones and session end.
