# engram-client

Python SDK for the [engram](https://github.com/iXanadu/engram) semantic memory API. Designed for Django/FastAPI web apps that need persistent AI memory with project and namespace isolation.

## Install

```bash
pip install -e /path/to/engram/integrations/python-client
```

Or from git:

```bash
pip install "engram-client @ git+https://github.com/iXanadu/engram.git#subdirectory=integrations/python-client"
```

Only dependency is `httpx`.

## Quick Start

```python
from engram_client import EngramClient

engram = EngramClient(
    url="http://localhost:8920",
    token="engram_...",           # principal's bearer token
    namespace="coursebuilder",    # your app's namespace (read/write boundary)
    project="ProjAlpha",         # current project context
)

# Search memories semantically
results = await engram.search("quiz generation strategies", limit=5)
for r in results:
    print(f"{r['key']}: {r['value'][:80]}")

# Store a memory
await engram.store("decision/quiz-format", "Multiple choice with explanations", tags="decision,quiz")

# Get by exact key
memory = await engram.get("decision/quiz-format")

# Delete
await engram.forget("decision/quiz-format")

# Always close when done (or use as context manager pattern)
await engram.close()
```

## Configuration

| Parameter | Required | Description |
|-----------|----------|-------------|
| `url` | Yes | Engram server URL (e.g. `http://localhost:8920`) |
| `token` | Yes | Bearer token tied to a principal (person or service) |
| `namespace` | Yes | Default namespace for writes. This is the access boundary — anyone with read access to a namespace can see all memories in it. |
| `project` | Yes | Default project name. Scopes memories within the namespace. |
| `read_namespaces` | No | Additional namespaces to include in searches. Useful when terminal (claude-code) and web app share memories during development. |
| `scope` | No | Default scope, almost always `"project"` (the default). |
| `timeout` | No | Request timeout in seconds (default: 30). |

## Cross-Namespace Reads

During development, you may co-develop with Claude Code at the terminal. Terminal memories go to namespace `claude-code`, web app memories go to your app's namespace. To search both:

```python
engram = EngramClient(
    url="http://localhost:8920",
    token="engram_...",
    namespace="coursebuilder",
    project="ProjAlpha",
    read_namespaces=["claude-code"],  # also search terminal memories
)

# Searches both "coursebuilder" AND "claude-code"
results = await engram.search("architecture decisions")
```

Writes always go to the primary `namespace`. Only reads fan out across `read_namespaces`.

## Django Integration

**settings.py:**

```python
ENGRAM_URL = env("ENGRAM_URL", default="http://localhost:8920")
```

**Account model** — store per-account engram config:

```python
class Account(models.Model):
    engram_token = models.CharField(max_length=100, blank=True)
    engram_namespace = models.CharField(max_length=100, blank=True)
    # ...
```

**Course model** — store the project name:

```python
class Course(models.Model):
    engram_project = models.CharField(max_length=100, blank=True)
    account = models.ForeignKey(Account, on_delete=models.CASCADE)
    # ...
```

**In your chat/AI view:**

```python
from engram_client import EngramClient

async def get_engram_client(account, course):
    return EngramClient(
        url=settings.ENGRAM_URL,
        token=account.engram_token,
        namespace=account.engram_namespace,
        project=course.engram_project,
    )
```

## Multi-Tenant Namespace Strategy

The namespace is the read boundary. Choose a strategy based on your isolation needs:

| Strategy | Namespace | Isolation | Use When |
|----------|-----------|-----------|----------|
| Flat | `coursebuilder` | None between accounts | Single-user / trusted |
| Per-account | `coursebuilder-{account}` | Full between accounts | Multi-tenant production |
| Per-project | `course-{name}` | Full between projects | Maximum isolation |

**Recommended for multi-tenant:** per-account namespace. All courses under one account share a namespace, so the AI can draw on cross-course knowledge within the same account. Different accounts are fully isolated.

The namespace is configurable per account, not hardcoded. During development, you can set your own account's namespace to `claude-code` to share memories with terminal Claude Code sessions.

## API Methods

### `search(query, *, project=None, scope=None, limit=5, namespaces=None)`

Semantic search across memories. Returns a list of matching items with scores.

### `store(key, value, *, tags="", project=None, scope=None, namespace=None, expiration_days=180)`

Store or update a memory. Returns the key.

### `get(key, *, project=None, scope=None, namespace=None)`

Retrieve a memory by exact key. Returns the item dict or `None`.

### `forget(key, *, project=None, scope=None, namespace=None)`

Delete a memory by key. Returns `True` if it existed.

### `health()`

Check server health. Returns `{"status": "ok", "checks": {...}}`.

All methods accept optional `project`, `scope`, and `namespace` overrides. When omitted, the client's defaults are used.

## Authentication

The token maps to a **principal** in engram's identity system. Principals represent people (human) or services (agent). The token determines:

- **Who owns the memory** — the principal's name is stored as `owner`
- **What namespaces are accessible** — read/write permissions on the principal
- **Admin access** — admins can access the management dashboard and API

Create and manage principals at the engram dashboard (`/dashboard` → Principals tab) or via the `/admin/principals` API.
