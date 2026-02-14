# Integrations

Engram is a standalone HTTP API. Any system that can make HTTP POST requests can use it as a memory backend.

## Available Integrations

- **[Claude Code](claude-code/)** — MCP server (engram-mcp) for Claude Code persistent memory
- **[Home Assistant](homeassistant/)** — Pyscript client + Blueprint for HA voice assistants

## Building a Custom Integration

Engram exposes four endpoints, all accepting JSON POST requests. The `namespace` field is **required** on all calls — it identifies which system is storing/querying memories.

| Endpoint | Purpose | Required Fields |
|----------|---------|-----------------|
| `POST /memory/set` | Store a memory | `namespace`, `key`, `value` |
| `POST /memory/get` | Retrieve by key | `namespace`, `key` |
| `POST /memory/search` | Semantic search | `namespace`, `query` |
| `POST /memory/forget` | Delete by key | `namespace`, `key` |

### Minimal Example (Python)

```python
import httpx

ENGRAM_URL = "http://localhost:8920"
NAMESPACE = "my-agent"

async def store_memory(key: str, value: str, tags: str = ""):
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{ENGRAM_URL}/memory/set", json={
            "namespace": NAMESPACE,
            "key": key,
            "value": value,
            "tags": tags,
            "user_id": "default",
        })
        return resp.json()

async def search_memories(query: str):
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{ENGRAM_URL}/memory/search", json={
            "namespace": NAMESPACE,
            "query": query,
            "user_id": "default",
        })
        return resp.json()["results"]
```

### Scoping

Every memory is scoped by three independent dimensions:

| Dimension | Purpose | Examples |
|-----------|---------|----------|
| `namespace` | Which system (required) | `claude-code`, `ha`, `my-agent` |
| `scope` | Visibility level | `shared`, `machine`, `project`, `user` |
| `user_id` | Identity within namespace | `global`, hostname, agent name |

Use these to isolate memories between different agents, users, and visibility levels.

### Authentication

If the server has `ENGRAM_API_TOKEN` set, include the token in all requests:

```
Authorization: Bearer <token>
```

The `/health` endpoint is always accessible without authentication.
