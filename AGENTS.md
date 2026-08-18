# engram

Generic semantic memory service for AI agents. FastAPI + pgvector + in-process sentence-transformers embeddings.

## Sources of Truth

- **`BACKLOG.md` (repo root)** — the single source of truth for OPEN work. Lean ledger, open items only, written as if public (no completed items, no vuln detail, no PII/topology/client names). Standard: `docs/backlog-standard.md`. READ IT EVERY SESSION.
- **Journal → engram memory** (`scope=project`): completed-item stories (`fix/<id>`), decisions (`decision/*`), lessons (`lesson/*`, scope=shared), long item detail (`backlog/<ID>`), open-vuln repro detail (`vuln/<ID>` until shipped). Memory-first; no state files.

## The Backlog — READ EVERY SESSION, SWEEP EVERY WRAPUP

`BACKLOG.md` is force-loaded each run. Triage every defect on discovery: BLOCKING (breaks build / wrong-or-harmful output / destroys data → fix now) vs DEGRADING (still does its job → add a ledger line and keep moving). If it's not in the ledger, it's not tracked. When idle, pull the top OPEN item. **Done = delete the line in the same commit as the fix** — never accumulate FIXED sections; the story goes to memory. Repo hygiene: `scripts/repo-hygiene-check.sh` must stay clean (assume-public doctrine).

## Project Structure

- `server/` — FastAPI app (config, db, embeddings, auth, routers, services)
  - `server/services/` — memory_service.py (all CRUD), principal_service.py (identity/auth)
  - `server/routers/` — memory.py, admin.py, principals.py
  - `server/auth.py` — PrincipalAuthMiddleware (two-mode: enrichment vs enforcement)
  - `server/dependencies.py` — auth helpers (get_current_principal, require_admin, check_namespace_access)
- `integrations/homeassistant/` — Pyscript client + Blueprint for HA voice assistants
- `integrations/claude-code/` — MCP bridge (engram-mcp) for Claude Code
- `scripts/` — install/start/restart/uninstall + migrate_ha_memory
- `launchd/` + `systemd/` — Service templates
- `tests/` — 88 tests (API, auth, admin, embeddings, e2e, memory_service, principal_service, principals_api, permissions, bootstrap)
- `docs/` — Getting-started, messaging, multi-provider, backlog standard, design/ (architecture, credentials); `docs/archive/` holds superseded-era docs

## Commands

- Run server: `uvicorn server.main:app --host 0.0.0.0 --port 8920`
- Run server tests: `pytest tests/ -v` (auto-isolates to `engram_test` DB; conftest creates it on first run with pgvector + pg_trgm)
- Run MCP tests: `cd integrations/claude-code && PYENV_VERSION=cc-memory-3.12 pytest tests/ -v`
- Health check: `curl http://localhost:8920/health`

## Conventions

- Config prefix: `ENGRAM_` (env vars and `.env` file)
- Database: `engram` (PostgreSQL + pgvector + pg_trgm)
- Default port: 8920
- Embedding model: nomic-ai/nomic-embed-text-v1.5 (in-process via sentence-transformers, no external service)
- pyenv virtualenv: `engram-3.12` (`.python-version` in repo)
- All memory CRUD goes through `server/services/memory_service.py`
- Fresh clone: `git config core.hooksPath .githooks` — arms the DEPLOY-4
  pre-push guard (refuses pushes while code trees are dirty)
- Schema auto-created on startup; migration SQL handles upgrades from older schemas

## Data Model

Three independent dimensions scope every memory:

| Dimension | Purpose | Examples |
|-----------|---------|----------|
| **namespace** | Which system (required, no default) | `fleet`, `ha`, `beast` |
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
- Dev (`~/projects/engram`) and prod (`/opt/srv/engram`) share the `engram-3.12` pyenv virtualenv, and only one can hold the editable install — last `pip install -e .` wins that mapping. **But the editable mapping does NOT decide which tree runs.** setuptools appends `_EditableFinder` to `sys.meta_path` *after* `PathFinder`, so anything reachable via `sys.path` beats it. In practice **CWD decides**: the LaunchDaemon sets `WorkingDirectory=/opt/srv/engram` and uvicorn does `sys.path.insert(0, app_dir)` with `app_dir` defaulting to `"."` (`uvicorn/main.py:528`), so prod imports `/opt/srv/engram/server` no matter where the editable mapping points. Verified 2026-08-13.
  - Corollary for your own shell: `python -c "import server"` resolves to **the tree you are standing in**, falling back to the editable mapping only from a neutral cwd. A bare import test therefore tells you about your cwd, not about prod — `cd /opt/srv/engram` first, or you will "discover" that prod runs dev's code and be wrong.
  - Prod's isolation is real but **incidental**: it rests entirely on that `WorkingDirectory`. If you ever change it, pass an explicit `--app-dir /opt/srv/engram`.
- Project `user_id` for `scope=project` is resolved via `.engram.cfg` walk-up (see `integrations/claude-code/src/engram_mcp/scoping.py`). Basename is only a fallback — required because server layouts like `/var/www/site/prod` would otherwise collide. Projects with a clean `~/projects/<name>/` layout commit `.engram.cfg` at the repo root; ambiguous layouts (nested, domain-style, separate prod/dev clones) leave it absent and let the `/startup` flow ask the user on first use. Templates never ship a `.engram.cfg`.

## Related Projects

- `integrations/claude-code/` — MCP bridge (engram-mcp) that CC uses to talk to engram. Installed in `cc-memory-3.12` pyenv. Replaces the old standalone `claude-memory-mcp` repo (archived).
- ha-semantic-memory — The original (deprecated, archived on GitHub 2026-02-13). Data migrated to engram DB.
- Global agent config templates live OUTSIDE this repo (operator's own fleet tooling) — the installer never touches provider-global config.

## State Management

**This project uses memory-first state tracking.** No `claude/CODEBASE_STATE.md` or `claude/session_progress/` files. All session state, decisions, and progress live in persistent memory at `scope=project`. Search memory on startup, store at milestones and session end.
