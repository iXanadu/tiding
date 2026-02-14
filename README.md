# Engram

Semantic memory service for AI agents. Store, search, and recall memories using hybrid vector + trigram search powered by PostgreSQL (pgvector) and Ollama embeddings.

Engram gives any AI agent persistent memory via a simple HTTP API. Originally built for Home Assistant voice assistants, it now serves Claude Code, custom agents, and any system that can make HTTP requests.

## How It Works

1. **Store** a memory with a key, value, and optional tags
2. Engram builds a search document from the key (expanded from snake_case), value, and tags
3. The document is embedded via Ollama (nomic-embed-text, 768 dimensions) and stored in PostgreSQL with pgvector
4. **Search** uses hybrid scoring: cosine similarity on the vector + pg_trgm trigram matching on the text, combined with configurable weights
5. Results are ranked by combined score, with configurable thresholds for both vector and trigram components

## Quick Start

### Prerequisites

- Python 3.12+ (via pyenv)
- PostgreSQL 17+ with [pgvector](https://github.com/pgvector/pgvector) and pg_trgm extensions
- [Ollama](https://ollama.ai) with `nomic-embed-text` model

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

# Ollama embedding model
ollama pull nomic-embed-text

# Configuration
cp .env.example .env
# Edit .env — for local PostgreSQL with peer auth, set:
#   ENGRAM_DB_USER=your_username
#   ENGRAM_DB_PASSWORD=

# Run
uvicorn server.main:app --host 0.0.0.0 --port 8920
```

### Docker (PostgreSQL only)

If you prefer Docker for PostgreSQL:

```bash
docker compose up -d
# Then run the server natively (Ollama needs GPU access)
uvicorn server.main:app --host 0.0.0.0 --port 8920
```

## API Reference

All endpoints accept JSON POST (except health).

### `GET /health`

Returns service status and dependency checks.

```json
{"status": "ok", "checks": {"postgres": true, "ollama": true}}
```

### `POST /memory/set`

Store or update a memory.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `namespace` | string | required | System identifier (`claude-code`, `ha`, etc.) |
| `key` | string | required | Unique identifier (snake_case recommended) |
| `value` | string | required | The memory content |
| `scope` | string | `"user"` | Visibility level (`shared`, `machine`, `project`, `user`) |
| `user_id` | string | `"default"` | Identity within namespace |
| `tags` | string | `""` | Comma-separated keywords for search boosting |
| `tags_search` | string | `""` | Additional search-optimized tags |
| `expiration_days` | int | `180` | Auto-expire after N days (0 = never) |

```bash
curl -X POST http://localhost:8920/memory/set \
  -H "Content-Type: application/json" \
  -d '{"namespace": "my-agent", "key": "user_location", "value": "Portland, OR", "tags": "home, address"}'
```

### `POST /memory/get`

Retrieve a memory by exact key.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `namespace` | string | required | System identifier |
| `key` | string | required | Exact key to look up |
| `scope` | string | `"user"` | Scope filter |
| `user_id` | string | `"default"` | Identity within namespace |

### `POST /memory/search`

Semantic search across memories.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `namespace` | string | required | System identifier |
| `query` | string | required | Natural language search query |
| `scope` | string | `"user"` | Scope filter |
| `user_id` | string | `"default"` | Identity within namespace |
| `limit` | int | `5` | Max results |

```bash
curl -X POST http://localhost:8920/memory/search \
  -H "Content-Type: application/json" \
  -d '{"namespace": "my-agent", "query": "where do I live", "limit": 3}'
```

### `POST /memory/forget`

Delete a memory by key.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `namespace` | string | required | System identifier |
| `key` | string | required | Key to delete |
| `scope` | string | `"user"` | Scope filter |
| `user_id` | string | `"default"` | Identity within namespace |

## Configuration

All settings use the `ENGRAM_` environment variable prefix. Set them in `.env` or as environment variables.

| Variable | Default | Description |
|----------|---------|-------------|
| `ENGRAM_DB_HOST` | `localhost` | PostgreSQL host |
| `ENGRAM_DB_PORT` | `5432` | PostgreSQL port |
| `ENGRAM_DB_NAME` | `engram` | Database name |
| `ENGRAM_DB_USER` | `engram` | Database user |
| `ENGRAM_DB_PASSWORD` | `engram` | Database password |
| `ENGRAM_OLLAMA_URL` | `http://localhost:11434` | Ollama API URL |
| `ENGRAM_EMBED_MODEL` | `nomic-embed-text` | Embedding model name |
| `ENGRAM_HOST` | `0.0.0.0` | Server bind address |
| `ENGRAM_PORT` | `8920` | Server port |
| `ENGRAM_LOG_LEVEL` | `info` | Log level |
| `ENGRAM_API_TOKEN` | _(empty)_ | Bearer token (empty = no auth) |
| `ENGRAM_VECTOR_THRESHOLD` | `0.35` | Minimum cosine similarity |
| `ENGRAM_TRIGRAM_WEIGHT` | `0.15` | Weight for trigram score in combined ranking |
| `ENGRAM_TRIGRAM_THRESHOLD` | `0.1` | Minimum trigram similarity |

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
        "memory_api_token": ""
      }
    }
  }
}
```

### Custom

Any HTTP client can use engram. See [integrations/README.md](integrations/README.md) for examples.

## Service Management

Scripts for running engram as a system service:

```bash
./scripts/install.sh    # Set up pyenv, deps, and launchd/systemd service
./scripts/start.sh      # Start the service
./scripts/restart.sh    # Restart the service
./scripts/uninstall.sh  # Stop and remove the service definition
```

The install script auto-detects macOS (LaunchDaemon) vs Linux (systemd).

## Testing

Requires a running PostgreSQL database (`engram`) and Ollama with `nomic-embed-text`:

```bash
pytest tests/ -v
```

The test suite includes:
- Unit tests for search text building and key expansion
- API integration tests (CRUD operations)
- Authentication middleware tests
- Embedding quality tests (cosine similarity thresholds)
- End-to-end semantic recall tests ("where do I live" -> `my_location`)

## Project Structure

```
engram/
├── server/                          # FastAPI application
│   ├── main.py                      # App setup, lifespan, middleware
│   ├── config.py                    # ENGRAM_ env var settings
│   ├── db.py                        # asyncpg pool, schema creation
│   ├── models.py                    # Pydantic request/response models
│   ├── embeddings.py                # Ollama embedding client
│   ├── auth.py                      # Principal auth middleware (two-mode)
│   ├── dependencies.py              # Auth helpers (require_admin, etc.)
│   ├── routers/
│   │   ├── memory.py                # /memory/* CRUD endpoints
│   │   ├── admin.py                 # /admin/memories management
│   │   ├── principals.py            # /admin/principals CRUD
│   │   └── health.py                # /health endpoint
│   └── services/
│       ├── memory_service.py        # Core CRUD + hybrid search logic
│       └── principal_service.py     # Identity + access control
├── integrations/
│   ├── homeassistant/               # HA Pyscript client + Blueprint
│   ├── claude-code/                 # MCP server for Claude Code (engram-mcp)
│   └── README.md                    # How to build a custom wrapper
├── scripts/                         # Service management (install/start/restart/uninstall)
├── launchd/com.engram.plist         # macOS LaunchDaemon template
├── systemd/engram.service           # Linux systemd unit template
├── tests/                           # pytest suite
├── docs/                            # System prompts, model selection guide
├── docker-compose.yml               # PostgreSQL + pgvector
├── pyproject.toml                   # Package definition
└── .env.example                     # Configuration template
```

## License

MIT
