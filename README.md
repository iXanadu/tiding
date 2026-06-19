# Engram

Semantic memory service for AI agents. Store, search, and recall memories using hybrid vector + trigram search powered by PostgreSQL (pgvector) and in-process sentence-transformers embeddings.

Engram gives any AI agent persistent memory via a simple HTTP API. Originally built for Home Assistant voice assistants, it now serves Claude Code, custom agents, and any system that can make HTTP requests.

## How It Works

1. **Store** a memory with a key, value, and optional tags
2. Engram builds a search document from the key (expanded from snake_case), value, and tags
3. The document is embedded via sentence-transformers (nomic-embed-text-v1.5, 768 dimensions) and stored in PostgreSQL with pgvector
4. **Search** uses hybrid scoring: cosine similarity on the vector + pg_trgm trigram matching on the text, combined with configurable weights
5. Results are ranked by combined score, with configurable thresholds for both vector and trigram components

## Data Model

Four independent dimensions partition every memory:

| Dimension | Purpose | Examples |
|-----------|---------|----------|
| **namespace** | Which system is writing | `claude-code`, `ha`, `beast`, `projbeta` |
| **scope** | Visibility level | `shared`, `machine`, `project`, `user`, `inbox` |
| **user_id** | Identity (the person, or machine for scope=machine) | `ixanadu`, hostname, `global`, HA UUID |
| **project** | Project name (only for `scope=project`) | `engram`, `projalpha`, `admin` |

UNIQUE constraint: `(namespace, key, scope, user_id, project)` with `NULLS NOT DISTINCT` — so two rows with the same key in the same namespace/scope/user_id but different projects coexist, while `project IS NULL` rows (scope=machine/shared/user/inbox) still enforce uniqueness on the four-tuple.

Each row also carries an `owner` column populated server-side from the authenticated principal on write — separate from `user_id` so an admin can read who-wrote-what without scanning metadata.

`namespace` is **required** on all API calls — there is no default. This forces every client to be explicit about which system it is.

### scope=project conventions

For `scope=project` writes, the canonical shape is:
- `user_id` = the **person** (principal name, e.g. `ixanadu`)
- `project` = the **project name** (e.g. `engram`, declared in `.engram.cfg`)

Pre-Phase-4 clients sometimes sent the project name in `user_id` with no `project` field. Those rows were backfilled in the 2026-05-12 migration (project ← old user_id, user_id ← owner). The MCP bridge sends the new shape after upgrade.

### Trust Model

**Namespace is the read boundary.** A principal with read access to a namespace can see every memory in it — all scopes, all user_ids. There is no project-level or scope-level read restriction within a namespace.

**The wrapper enforces write targeting.** Each integration (MCP bridge, web app, pyscript) acts as a harness that resolves the correct scope and user_id for the current context. For example, the Claude Code MCP bridge resolves project identity from `.engram.cfg` in the repo root and injects `scope=project, user_id={person}, project={project_name}` on every call. The wrapper prevents accidents; the server prevents unauthorized access.

**Multi-namespace search** enables cross-system collaboration. The search endpoint accepts a `namespaces` array, so one AI can read memories written by another AI system while preserving provenance (the `namespace` field on each result shows who wrote it).

## Access Control

### Without Auth (default)

Set `ENGRAM_API_TOKEN` to a Bearer token. All requests must include it. Leave empty for no auth.

### With Principals (recommended for multi-client)

Enable `ENGRAM_REQUIRE_AUTH=true` for principal-based authentication. Each client gets its own identity with explicit namespace permissions:

```
claude-code:    read: [claude-code, claude-web]    write: [claude-code]
projbeta:   read: [projbeta]                write: [projbeta]
ha-system:      read: [ha]                          write: [ha]
```

Principal types:
- **agent** — AI systems, services. Authenticates via Bearer token (`engram_<random>`, bcrypt-hashed in DB).
- **human** — People. Can have a token and/or password. Admin flag grants access to `/admin/*` endpoints.

A bootstrap admin is auto-created from `ENGRAM_API_TOKEN` when `require_auth=true` and no admins exist.

### Dry-Run Mode

Set `ENGRAM_WARN_UNAUTHED=true` to log warnings for unauthenticated requests without blocking them. Useful for auditing before flipping enforcement on.

## Quick Start

### Prerequisites

- Python 3.12+ (via pyenv)
- PostgreSQL 17+ with [pgvector](https://github.com/pgvector/pgvector) and pg_trgm extensions

### Setup

```bash
# Clone and enter
git clone https://github.com/ixanadu/engram.git
cd engram

# Python environment
pyenv virtualenv 3.12 engram-3.12
pyenv local engram-3.12
pip install -e ".[dev]"

# Database
createdb engram
# (Tables and indexes are created automatically on first run)

# Configuration
cp .env.example .env
# Edit .env — for local PostgreSQL with peer auth, set:
#   ENGRAM_DB_USER=your_username
#   ENGRAM_DB_PASSWORD=

# Run
uvicorn server.main:app --host 0.0.0.0 --port 8920
```

The embedding model (nomic-ai/nomic-embed-text-v1.5, ~270MB) is downloaded automatically on first start and cached in `~/.cache/huggingface/`. No external services required — embeddings run in-process using MPS (Apple Silicon GPU) or CPU.

### Docker (PostgreSQL only)

If you prefer Docker for PostgreSQL:

```bash
docker compose up -d
# Then run the server natively
uvicorn server.main:app --host 0.0.0.0 --port 8920
```

## API Reference

All memory endpoints accept JSON POST. Admin and principal endpoints use standard REST verbs.

### Health

`GET /health` — Service status and dependency checks.

```json
{"status": "ok", "checks": {"postgres": true, "embeddings": true}}
```

### Memory CRUD

#### `POST /memory/set`

Store or update a memory.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `namespace` | string | required | System identifier |
| `key` | string | required | Unique identifier (snake_case recommended) |
| `value` | string | required | The memory content |
| `scope` | string | `"user"` | Visibility level (`shared`, `machine`, `project`, `user`) |
| `user_id` | string | `"default"` | Identity (the person — required when scope=project) |
| `project` | string | `null` | Project name (required when scope=project, null for other scopes) |
| `tags` | string | `""` | Comma-separated keywords for search boosting |
| `tags_search` | string | `""` | Additional search-optimized tags |
| `expiration_days` | int | `0` | `0` = never expires (default — engram is a durable store). Set a positive value only for genuinely ephemeral memories. |

```bash
curl -X POST http://localhost:8920/memory/set \
  -H "Content-Type: application/json" \
  -d '{"namespace": "my-agent", "key": "user_location", "value": "Portland, OR", "tags": "home, address"}'
```

#### `POST /memory/get`

Retrieve a memory by exact key.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `namespace` | string | required | System identifier |
| `key` | string | required | Exact key to look up |
| `scope` | string | `"user"` | Scope filter |
| `user_id` | string | `"default"` | Identity within namespace |
| `project` | string | `null` | Project name (for scope=project lookups) |

#### `POST /memory/search`

Semantic search across memories. Supports single, multi-, or implicit-namespace queries.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `namespace` | string | optional | Single namespace to search |
| `namespaces` | string[] | optional | Multiple namespaces to search |
| `query` | string | required | Natural language search query |
| `scope` | string | `"user"` | Scope filter |
| `user_id` | string | `"default"` | Identity within namespace |
| `project` | string | `null` | Project filter (for scope=project queries) |
| `limit` | int | `5` | Max results |

When **neither** `namespace` nor `namespaces` is provided, the server resolves the search to the authenticated principal's `read_namespaces` (expanding `*` to all concrete namespaces). Anonymous callers without a principal get 401 in that case. Explicit namespace/namespaces still works for back-compat.

```bash
curl -X POST http://localhost:8920/memory/search \
  -H "Content-Type: application/json" \
  -d '{"namespace": "my-agent", "query": "where do I live", "limit": 3}'
```

#### `POST /memory/forget`

Delete a memory by key.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `namespace` | string | required | System identifier |
| `key` | string | required | Key to delete |
| `scope` | string | `"user"` | Scope filter |
| `user_id` | string | `"default"` | Identity within namespace |
| `project` | string | `null` | Project name (for scope=project deletes) |

### Caller-Scoped Identity Endpoints

These let any authenticated client discover its own principal and capabilities without an admin token.

#### `GET /whoami`

Returns the principal record for the bearer token attached to the request.

```json
{
  "id": "...", "name": "ixanadu", "type": "human", "is_admin": true,
  "has_token": true, "has_password": false,
  "read_namespaces": ["*"], "write_namespaces": ["*"],
  "active": true, "created_at": "..."
}
```

Returns 401 for anonymous callers / invalid tokens.

#### `GET /namespaces`

Returns the namespaces the caller can read and write. Wildcards in the principal's permissions are expanded server-side to concrete namespaces present in the DB.

```json
{
  "status": "ok",
  "read":  ["claude-code", "beastchat", "ha", ...],
  "write": ["claude-code", "beastchat", "ha", ...]
}
```

Closes the wildcard-expansion gap consumer apps used to hit by calling `/admin/stats` (which required an admin token).

### Inbox (Inter-Agent Messaging)

Built on top of the memory table. Enables Claude Code sessions (or any agent) to leave messages for each other across sessions and machines.

- `POST /memory/send` — Send a message to an address
- `POST /memory/inbox` — List inbox messages for a set of listen addresses
- `POST /memory/inbox/{id}/ack` — Mark a message as read (per-reader)
- `POST /memory/inbox/{id}/archive` — Archive a message (global hide)

**Addressing.** An address is a flat string; a session listens on a *set* of them (its `listen_set`) and is reachable on any:

- `<project>` — loose **group** address: every session on that project
- `machine:<host>` — every session on that machine
- `<project>@<host>` — that one session, **precisely**

Replies (`memory_reply`) thread automatically and route back to the sender's group address.

**Per-session identity.** Two sessions on one project — e.g. a web backend and a native app sharing one `.engram.cfg` — share project *memory* but need distinct inbox identities so they can DM each other without cross-waking. Declare an identity and the session is addressed as `<name>@<host>` while still joining the `<project>` group for broadcasts. Memory scoping is unaffected (it stays `project`-derived). Two sources, env winning as an override:

```ini
# .engram.cfg — preferred: per-repo, version-controlled, resolved from the
# session's working dir so the MCP send path AND the watcher both pick it up
# automatically (the claude-memory MCP server is registered once, globally,
# so there is no per-session env block to rely on).
project       = beastchat          # shared memory bucket — same for both sessions
inbox_identity = beastchat-server  # distinct inbox address — different per session
```

```bash
# ENGRAM_INBOX_IDENTITY env var — overrides the file when set (escape hatch)
export ENGRAM_INBOX_IDENTITY=beastchat-server
```

**Auto-wake watcher (`engram-inbox-wait`).** A dormant session never learns a reply arrived — it only resumes when the human types. The `engram-inbox-wait` console script (installed with the [Claude Code bridge](#claude-code)) polls the inbox and emits one line per new message, which the Claude Code harness turns into a wake-up:

```bash
# always-on: arm at session start under the Monitor tool, one wake per message
engram-inbox-wait --follow --project-dir /path/to/repo

# one-shot: exits on the first new message (Bash background → single wake)
engram-inbox-wait --project-dir /path/to/repo
```

It authenticates from `~/.config/engram/identity` (a bare shell doesn't inherit the bridge's env). It seeds on the existing backlog so it only wakes on mail arriving *after* it starts, and it drops your own outbound (`from == self`) so you never wake on your own sends.

### Admin Endpoints

Require admin principal when `require_auth=true`. Open otherwise.

- `GET /admin/memories` — List/browse memories with filtering, pagination, sorting. Supports comma-separated namespaces.
- `GET /admin/machines` — List unique machine identifiers from metadata.
- `GET /admin/stats` — Namespace counts with optional scope breakdown.
- `PATCH /admin/memories` — Update a memory's namespace, scope, user_id, key, or tags.
- `POST /admin/bulk-delete` — Delete memories by key prefix with safety guard.
- `POST /admin/cleanup` — Manually trigger expiration cleanup.

### Principal Management

All under `/admin/principals`. Require admin when `require_auth=true`.

- `POST /admin/principals` — Create a principal (returns raw token once).
- `GET /admin/principals` — List principals (filterable by type, active status).
- `GET /admin/principals/{name}` — Get a specific principal.
- `PATCH /admin/principals/{name}` — Update permissions, status, token/password.
- `DELETE /admin/principals/{name}` — Deactivate a principal.
- `POST /admin/principals/{name}/token` — Regenerate token.
- `POST /admin/principals/{name}/aliases` — Add an alias (e.g., HA UUID → principal name).
- `GET /admin/principals/{name}/aliases` — List aliases.
- `DELETE /admin/principals/{name}/aliases` — Remove an alias.

### Web UIs

- `GET /dashboard` — Memory browser with search, filtering, stats, and edit modal.
- `GET /bridge` — Standalone memory search UI with cross-namespace provenance badges.

Both pages are auth-exempt at the middleware level, but API calls from them require tokens when `require_auth=true` (the bridge UI stores the token in localStorage).

## Configuration

All settings use the `ENGRAM_` environment variable prefix. Set them in `.env` or as environment variables.

| Variable | Default | Description |
|----------|---------|-------------|
| `ENGRAM_DB_HOST` | `localhost` | PostgreSQL host |
| `ENGRAM_DB_PORT` | `5432` | PostgreSQL port |
| `ENGRAM_DB_NAME` | `engram` | Database name |
| `ENGRAM_DB_USER` | `engram` | Database user |
| `ENGRAM_DB_PASSWORD` | `engram` | Database password |
| `ENGRAM_EMBED_MODEL` | `nomic-ai/nomic-embed-text-v1.5` | HuggingFace embedding model |
| `ENGRAM_HOST` | `0.0.0.0` | Server bind address |
| `ENGRAM_PORT` | `8920` | Server port |
| `ENGRAM_LOG_LEVEL` | `info` | Log level |
| `ENGRAM_API_TOKEN` | _(empty)_ | Legacy Bearer token (empty = no auth) |
| `ENGRAM_REQUIRE_AUTH` | `false` | Enable principal-based authentication |
| `ENGRAM_WARN_UNAUTHED` | `false` | Log warnings for unauthenticated requests |
| `ENGRAM_CLEANUP_ENABLED` | `true` | Run background expiration cleanup. **Disabled in production** — engram is manual-curation; nothing is auto-deleted. See note below. |
| `ENGRAM_CLEANUP_INTERVAL_HOURS` | `6` | Hours between cleanup runs |
| `ENGRAM_CLEANUP_BATCH_SIZE` | `500` | Max expired records per cleanup run |
| `ENGRAM_VECTOR_THRESHOLD` | `0.35` | Minimum cosine similarity |
| `ENGRAM_TRIGRAM_WEIGHT` | `0.15` | Weight for trigram score in combined ranking |
| `ENGRAM_TRIGRAM_THRESHOLD` | `0.1` | Minimum trigram similarity |

> **Expiry / cleanup posture:** memories are **permanent by default** (`expiration_days=0`) and the background cleanup task is **off in production** (`ENGRAM_CLEANUP_ENABLED=false`), so `expires_at` is inert — nothing is auto-deleted regardless of what a client sends. This matches engram's curate-deliberately model. The cleanup task is being reconsidered as a *consolidation* mechanism (detect stale/duplicate memories, summarize, then prune) rather than a blunt TTL deleter. Re-enabling auto-expiry requires both flipping `ENGRAM_CLEANUP_ENABLED` and having clients set deliberate short TTLs.

## Search Algorithm

Engram uses a hybrid search that combines two signals:

1. **Vector search** — Cosine similarity between the query embedding and stored memory embeddings (pgvector HNSW index)
2. **Trigram search** — Character-level fuzzy matching via PostgreSQL's pg_trgm extension

The search flow:
1. Find the top `limit * 3` memories by vector similarity
2. Compute trigram similarity for each candidate
3. Combined score = `vec_score + (trigram_weight * trgm_score)`
4. Filter: keep results where `vec_score >= vector_threshold` OR `trgm_score >= trigram_threshold`
5. Return top `limit` results sorted by combined score

This hybrid approach handles both semantic queries ("where do I live") and exact/fuzzy matches ("Portland") well.

## Integrations

### Home Assistant

Pyscript client + Blueprint for HA voice assistants. See [integrations/homeassistant/](integrations/homeassistant/).

### Claude Code

MCP server for Claude Code lives in [integrations/claude-code/](integrations/claude-code/). Install it in its own pyenv virtualenv:

```bash
pyenv virtualenv 3.12 cc-memory-3.12
cd integrations/claude-code
PYENV_VERSION=cc-memory-3.12 pip install -e .
```

Then register in `~/.claude.json`:

```json
{
  "mcpServers": {
    "claude-memory": {
      "type": "stdio",
      "command": "path/to/pyenv/versions/cc-memory-3.12/bin/python",
      "args": ["-m", "engram_mcp.server"],
      "env": {
        "memory_api_url": "http://localhost:8920",
        "memory_api_token": "engram_<your-token>"
      }
    }
  }
}
```

> **Recommended: keep the token out of `~/.claude.json`.** Claude Code rewrites that file, so put the token in `~/.config/engram/identity` instead (`.env`-style, `chmod 600`) and omit it from the `.claude.json` env block:
>
> ```
> memory_api_token=engram_<your-token>
> memory_api_url=http://localhost:8920
> memory_namespace=claude-code
> ```
>
> The bridge reads it from there when the `.claude.json` env block omits the token. (An inline env token still works and takes precedence — but a fragile config file is a poor home for a credential.)

The MCP bridge resolves project identity from `.engram.cfg` in the repo root (walk-up search). Create one in each project:

```
# .engram.cfg
project = my-project-name
```

**Tools the bridge exposes:** `memory_store`, `memory_search`, `memory_get`, `memory_forget`, `memory_send`/`memory_inbox`/`memory_reply`/`memory_ack`/`memory_inbox_archive` (inter-agent inbox), `memory_status` (health), `memory_declare_identity`, and **`memory_whoami`** — which reports the session's principal and the namespaces it can read/write (wildcards expanded). An agent can call `memory_whoami` to discover its own reach rather than being told in a prompt.

The bridge also installs the **`engram-inbox-wait`** console script — arm it at session start (under Claude Code's Monitor tool) so the session wakes on new inbox mail without a human relaying it. See [Inbox → Auto-wake watcher](#inbox-inter-agent-messaging).

**Search is permission-driven.** By default (`memory_read_namespaces` empty) the bridge sends *no* namespace on search, so the server returns results from every namespace the **token** can read — grant a principal read of another namespace and it shows up automatically, no client config change. Set `memory_read_namespaces` to a CSV only if you want to *narrow* below the token's permissions.

> **Use a scoped principal for the bridge, not an admin/wildcard token.** The bridge's reach *is* the token's read permissions, so give it a principal scoped to exactly the namespaces it should see (e.g. `claude-code` reading `claude-code, claude-web, grok`). An admin (`*.*`) token would make every search span *all* namespaces and put a god credential on every box.

### Python Client SDK (web apps)

`integrations/python-client/` ships `engram-client` — an async SDK for web/app backends (FastAPI, Django) that need memory:

```bash
pip install -e path/to/engram/integrations/python-client
```

```python
from engram_client import EngramClient

engram = EngramClient.from_env("MYAPP")   # reads MYAPP_ENGRAM_{URL,TOKEN,NAMESPACE,...}
if await engram.is_available():           # kill-switch-aware health probe; never raises
    await engram.store("decision/quiz-format", "multiple choice")  # durable by default
    hits = await engram.search("quiz strategies")
```

The full conventions — namespace-per-person, per-user token handling, expiry discipline, FastAPI wiring, and migration steps — are in **[docs/webapp-integration-spec.md](docs/webapp-integration-spec.md)**. The package also installs an `engram` CLI for provisioning principals:

```bash
engram principal create <name> --write <ns> --read <ns,...>   # mints a token, shown once
```

### Custom

Any HTTP client can use engram. See [integrations/README.md](integrations/README.md) for examples.

## Service Management

Scripts for running engram as a system service:

```bash
./scripts/install.sh    # Set up pyenv, deps, and launchd/systemd service
./scripts/start.sh      # Start the service
./scripts/stop.sh       # Stop the service
./scripts/restart.sh    # Restart the service
./scripts/uninstall.sh  # Stop and remove the service definition
```

The install script auto-detects macOS (LaunchDaemon) vs Linux (systemd).

## Testing

Tests run against an **isolated** `engram_test` database — never the production `engram` DB. The conftest creates `engram_test` on first run (installs pgvector + pg_trgm) and hard-asserts the test session is targeting it. This is enforced because some test fixtures (e.g. `_cleanup_inbox`) issue `DELETE FROM memories` and would obliterate real data on a shared host.

```bash
pytest tests/ -v
```

The test suite includes:
- Unit tests for search text building and key expansion
- API integration tests (CRUD operations)
- Authentication and principal middleware tests
- Namespace permission enforcement tests
- Embedding quality tests (cosine similarity thresholds)
- End-to-end semantic recall tests ("where do I live" -> `my_location`)
- Admin endpoint and bulk operation tests
- Principal CRUD and alias tests

## Project Structure

```
engram/
├── server/                          # FastAPI application
│   ├── main.py                      # App setup, lifespan, CORS, middleware
│   ├── config.py                    # ENGRAM_ env var settings
│   ├── db.py                        # asyncpg pool, schema creation
│   ├── models.py                    # Pydantic request/response models
│   ├── embeddings.py                # sentence-transformers embedding client
│   ├── auth.py                      # Principal auth middleware (two-mode)
│   ├── dependencies.py              # Auth helpers (require_admin, namespace checks)
│   ├── routers/
│   │   ├── memory.py                # /memory/* CRUD + inbox endpoints
│   │   ├── admin.py                 # /admin/* memory management
│   │   ├── principals.py            # /admin/principals CRUD
│   │   ├── dashboard.py             # /dashboard and /bridge web UIs
│   │   └── health.py                # /health endpoint
│   ├── services/
│   │   ├── memory_service.py        # Core CRUD + hybrid search + inbox logic
│   │   ├── principal_service.py     # Identity + access control
│   │   ├── admin_service.py         # List, stats, bulk delete, cleanup
│   │   ├── cleanup_task.py          # Background expiration loop
│   │   ├── identity.py              # Address resolution and validation
│   │   └── inbox_guidance.py        # Usage hints for inbox operations
│   └── templates/
│       ├── dashboard.html           # Memory browser UI
│       └── bridge.html              # Cross-namespace search UI
├── integrations/
│   ├── homeassistant/               # HA Pyscript client + Blueprint
│   ├── claude-code/                 # MCP server for Claude Code (engram-mcp)
│   └── README.md                    # How to build a custom wrapper
├── scripts/                         # Service management (install/start/stop/restart/uninstall)
├── launchd/com.engram.plist         # macOS LaunchDaemon template
├── systemd/engram.service           # Linux systemd unit template
├── tests/                           # pytest suite
├── docs/                            # System prompts, guides
├── docker-compose.yml               # PostgreSQL + pgvector
├── pyproject.toml                   # Package definition
└── .env.example                     # Configuration template
```

## License

MIT
