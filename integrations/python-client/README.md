# engram-client

Python SDK for the [engram](https://github.com/iXanadu/tiding) semantic memory API. Designed for Django/FastAPI web apps that need persistent AI memory with project and namespace isolation.

## Install

```bash
pip install -e /path/to/engram/integrations/python-client
```

Or from git:

```bash
pip install "engram-client @ git+https://github.com/iXanadu/tiding.git#subdirectory=integrations/python-client"
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
| `project` | Yes | Default project name. Sent in the dedicated `project` column (Phase 4+). |
| `user_id` | No | The **writing principal** whose rows you want. For `scope="user"` that is you; for `scope="project"` it is usually **another** agent (`claude-code`, `grok`) — see [Reading another agent's project notes](#reading-another-agents-project-notes). Matched EXACTLY, no wildcard. When omitted the SDK calls `/whoami` once and uses your own principal, which for an app usually owns no project rows. |
| `read_namespaces` | No | Additional namespaces to include in searches. ⚠️ Every entry must be readable by your token — one unreadable entry currently zeroes the whole result silently. Prefer omitting it and letting the server resolve from token permissions. |
| `scope` | No | Default scope, almost always `"project"` (the default). |
| `timeout` | No | Request timeout in seconds (default: 30). |

## Namespace and user_id are DIFFERENT AXES

Read this before writing any cross-agent search. Conflating these two is the
single most common integration bug, and this README used to encourage it.

| axis | means | example |
|---|---|---|
| `namespace` | **WHERE** rows live — the access boundary | `fleet` |
| `user_id` | **WHO WROTE** them — the writing principal | `claude-code`, `grok` |

They used to be the same string, so "namespace == principal name" worked as a
convention. **It is no longer true.** Coding-agent notes are written by
principals `claude-code` and `grok` into namespace `fleet`. A client that sends
`namespace=<principal>` silently returns nothing the day that stops matching —
and returns nothing *right now* if it sends `namespace=fleet, user_id=fleet`,
because `fleet` is a namespace and has never been a writer.

## Reading another agent's project notes

The common case: a web app answering "what's the status of project X?" from the
coding agents' notes.

```python
# DO NOT send a namespace. The server resolves namespaces from your token's
# read permissions, so this keeps working across renames with no client change.
results = await engram.search(
    "what's the status?",
    scope="project",
    project="projalpha",     # lowercased
    user_id="claude-code",   # the WRITER whose notes you want — usually NOT you
)
```

⚠️ **`user_id` is matched EXACTLY — there is no "all writers" wildcard.** To
read notes from more than one agent, make one call per writer and merge:

```python
for writer in ("claude-code", "grok"):
    results += await engram.search(..., user_id=writer)
```

Omitting `user_id` does **not** mean "any writer": the SDK falls back to your
own principal, which for a web app owns no project rows — so you get zero.

## Cross-Namespace Reads

`read_namespaces` fans reads out beyond your primary namespace. Writes always go
to the primary `namespace`; only reads fan out.

```python
engram = EngramClient(
    url="http://localhost:8920", token="engram_...",
    namespace="coursebuilder", project="ProjAlpha",
    read_namespaces=["fleet"],   # also search the coding agents' namespace
)
```

⚠️ **Every namespace you list must be readable by your token.** A single
unreadable entry currently zeroes the ENTIRE result — a 200 with no rows, not a
403 — so a generous list returns less than a narrow one, silently. List only
what you know the token can read, or omit the parameter entirely and let the
server resolve it for you (preferred).

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

The namespace is configurable per account, not hardcoded. During development you
can point your own account's namespace at `fleet` to share memories with
terminal coding sessions — that is the namespace they write to (it was called
`claude-code` before the provider-neutral rename; an alias still maps the old
name, but do not build on it).

⚠️ Sharing a namespace only gets you the same *access boundary*. To read what a
coding agent actually wrote under `scope="project"`, you must also pass its
`user_id` — see [Reading another agent's project notes](#reading-another-agents-project-notes).

## API Methods

### `search(query, *, project=None, scope=None, limit=5, namespaces=None)`

Semantic search across memories. Returns a list of matching items with scores.

### `store(key, value, *, tags="", project=None, scope=None, namespace=None, expiration_days=0)`

Store or update a memory. Returns the key. `expiration_days=0` (default) means it never expires — engram is a durable store; pass a positive value only for genuinely ephemeral memories.

### `get(key, *, project=None, scope=None, namespace=None)`

Retrieve a memory by exact key. Returns the item dict or `None`.

### `forget(key, *, project=None, scope=None, namespace=None)`

Delete a memory by key. Returns `True` if it existed.

### `health()` / `is_available()`

`health()` returns `{"status": "ok", "checks": {...}}`. `is_available()` is a never-raising convenience: returns `True` only if the client is `enabled` and the server is healthy — use it to gate memory-dependent paths and degrade gracefully.

### `whoami()` / `namespaces()`

Discover the token's identity and reach. `whoami()` returns the principal record (name, type, admin flag, raw read/write lists). `namespaces()` returns `{"read": [...], "write": [...]}` with wildcards expanded to concrete namespaces — use it to show a user what their assistant can recall.

### `EngramClient.from_env(prefix)`

Classmethod that builds a client from `<PREFIX>_ENGRAM_{URL,TOKEN,NAMESPACE,PROJECT,SCOPE,ENABLED}` environment variables (SCOPE defaults to `user`).

All read/write methods accept optional `project`, `scope`, and `namespace` overrides. When omitted, the client's defaults are used.

## Authentication

The token maps to a **principal** in engram's identity system. Principals represent people (human) or services (agent). The token determines:

- **Who owns the memory** — the principal's name is stored as `owner`
- **What namespaces are accessible** — read/write permissions on the principal
- **Admin access** — admins can access the management dashboard and API

Create and manage principals at the engram dashboard (`/dashboard` → Principals tab) or via the `/admin/principals` API.
