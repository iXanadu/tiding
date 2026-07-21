> **ARCHIVED (2026-07-20).** Original design spec for the principals/auth system. Phases 1–2 shipped (in places differently than spec'd — e.g. the `human` namespace concept was superseded by the unified-namespace + owner-token model); Phase 3 was closed as unnecessary for single-operator deployments. Current truth: `docs/messaging.md` and `docs/multi-provider.md`. Kept for design history; not maintained.

# Principals: Identity & Access Control for engram

## The Problem

engram stores memories from multiple systems (HA, Claude Code, Beast, future chat apps). Today, access control is a single shared API token — if you have it, you see everything. There's no concept of *who* is asking.

This creates three gaps:

1. **The dashboard can't search across namespaces.** A human admin wants to type "children" and find results whether that memory was stored via HA, Claude, or Beast. Today, each query requires exactly one namespace.

2. **There's no read/write asymmetry.** Writing is naturally scoped — Claude Code writes to `claude-code`, Beast writes to `beast`. But reading is different. The human who *instructed* these agents should see all their work. A different human should only see their own.

3. **Agents have no identity.** Beast spawns Claude Code sessions, which write memories. Beast might need to read what its spawned sessions produced. Today there's no way to express "Beast can read `claude-code` but not `human`."

## Key Insight: Channels vs Namespaces

The original design treated HA as both a system *and* the home for human memories (`namespace=ha`). But HA is just one **channel** — one of many ways a human interacts with AI. A human might also:

- Use a chat app (web, mobile)
- Forward an email to an agent for summarization
- Talk to a CLI tool
- Use the engram dashboard directly

If human memories live in the `ha` namespace, switching away from HA means orphaned memories. The fix:

**Human memories get their own namespace: `human`.** The channel that captured the memory (HA, chat, email, CLI) is metadata — not the organizing principle.

```
Before (channel = namespace, fragile):
  HA voice    → namespace: ha,    user_id: ha-user-uuid
  Chat app    → namespace: chat,  user_id: ix@email.com
  Email agent → namespace: ???

After (human-centric, channel is metadata):
  HA voice    → namespace: human, user_id: ixanadu, channel: ha
  Chat app    → namespace: human, user_id: ixanadu, channel: chat
  Email agent → namespace: human, user_id: ixanadu, channel: email

  HA system   → namespace: ha  (automations, device data — not personal memories)
```

This means:
- Humans register as principals independent of any system
- Drop HA tomorrow — your memories are still in `human`, intact
- Add a new chat app — it writes to `human` with `channel: chat`, shows up alongside everything else
- The `ha` namespace still exists for HA *system* things (automation state, device configs), not personal memories

## What Doesn't Change

- **The `memories` table schema stays the same.** Namespace, scope, user_id, key, value — all unchanged. Channel information can be stored in the existing `tags` field (e.g. `channel:ha`) or as a key prefix convention.
- **Existing API contracts stay the same.** `/memory/set`, `/memory/get`, `/memory/search`, `/memory/forget` keep their current signatures.
- **Scope still means visibility level** within a namespace (shared/machine/project/user).

## What Changes

- **Namespace meaning expands.** It still answers "which system" for agents (`claude-code`, `beast`). For humans, it answers "whose kind of memory" — `human` for personal memories, distinct from agent working memory.
- **New tables** for principals, aliases, consent, and audit logging.
- **Auth middleware** resolves tokens/sessions to principals and enforces read/write permissions.

## Core Concept: Principals

A **principal** is a real identity that can act across namespaces. Humans are principals. Agents are principals. Each principal authenticates (token, password, session) and gets a set of permissions.

### Principal Types

| Type | Examples | How they authenticate | Typical access |
|------|----------|----------------------|----------------|
| `human` | ixanadu, wife | Dashboard login, or identified by channel (HA user_id, chat username) | Read/write own `human` memories; may have read access to agent namespaces |
| `agent` | claude-code, beast | API bearer token | Read/write own namespace, maybe read others |
| `admin` | ixanadu (wearing admin hat) | Dashboard login | Read/write agent namespaces freely; human memories still protected (see privacy model) |

A principal can be both `human` and `admin`. These aren't mutually exclusive — it's about which hat they're wearing.

### Aliases

A single human is known by different identifiers across channels:

- HA knows them as a UUID (`context.user_id`)
- A chat app knows them by username or email
- The dashboard knows them by login name

Aliases map all these external identifiers back to one principal. When any channel sends a request containing an identifier matching a known alias, engram resolves it to the principal.

| Channel | External identifier | Resolves to |
|---------|-------------------|-------------|
| HA voice | `a1b2c3d4-uuid` | → ixanadu |
| Chat app | `ix@email.com` | → ixanadu |
| Dashboard | login session | → ixanadu |
| HA voice | `e5f6g7h8-uuid` | → wife |

## Data Model

### `principals` table

| Column | Type | Notes |
|--------|------|-------|
| `id` | uuid | Primary key |
| `name` | text | Unique. "ixanadu", "claude-code", "beast" |
| `type` | text | "human", "agent". Admins are humans with `is_admin=true` |
| `is_admin` | bool | Grants read/write to all agent namespaces. Does NOT grant access to other humans' memories |
| `token_hash` | text | bcrypt hash of API token (agents + optional for humans) |
| `password_hash` | text | bcrypt hash of password (humans, dashboard login) |
| `read_namespaces` | text[] | Namespaces this principal can read, or `{"*"}` for all agent namespaces |
| `write_namespaces` | text[] | Namespaces this principal can write to |
| `active` | bool | Soft disable without deleting |
| `created_at` | timestamptz | |

**Note on `human` namespace access**: every human principal implicitly has read/write access to `namespace=human` filtered to their own `user_id`. This doesn't need to be in `read_namespaces`/`write_namespaces` — it's inherent to being a human principal.

### `principal_aliases` table

| Column | Type | Notes |
|--------|------|-------|
| `id` | uuid | Primary key |
| `principal_id` | uuid | FK → principals |
| `alias` | text | The external identifier ("a1b2c3d4-uuid", "ix@email.com") |
| `source` | text | Which channel ("ha", "email", "chat", "cli") |
| UNIQUE | | `(alias, source)` |

### `consent_grants` table

| Column | Type | Notes |
|--------|------|-------|
| `id` | uuid | Primary key |
| `granter_id` | uuid | FK → principals. The human sharing access |
| `grantee_id` | uuid | FK → principals. Who receives access |
| `granted_at` | timestamptz | |
| `expires_at` | timestamptz | Null = no expiry. Auto-revoke after this time |
| `revoked_at` | timestamptz | Null until revoked |

Note: consent grants apply to the granter's `human` namespace memories only. No need for a namespace list — a human's private memories are always in `human` under their `user_id`.

### `audit_log` table

| Column | Type | Notes |
|--------|------|-------|
| `id` | uuid | Primary key |
| `principal_id` | uuid | FK → principals. Who performed the action |
| `action` | text | "break_glass_read", "consent_grant", "consent_revoke", etc. |
| `target_principal_id` | uuid | Whose data was accessed (null for non-human-privacy actions) |
| `detail` | text | Optional. Reason for break-glass, or other context |
| `created_at` | timestamptz | |

## Namespace Map

After this change, namespaces have clear purposes:

| Namespace | Type | Contains | Who writes | Who reads |
|-----------|------|----------|------------|-----------|
| `human` | personal | Human memories from any channel | Humans (via HA, chat, email, CLI, dashboard) | Each human sees only their own; admins cannot without consent/break-glass |
| `claude-code` | agent | CC session state, lessons, fixes | Claude Code (MCP bridge) | CC itself; Beast (read); admin principals |
| `beast` | agent | Beast operational state (future) | Beast | Beast itself; admin principals |
| `ha` | system | HA automations, device data, system state | HA integration | HA itself; admin principals |
| `quant-bot` | agent | Trading research, market analysis | QuantBot agent | QuantBot itself; admin principals |

**The rule**: agent/system namespaces are open to any principal with read permission. The `human` namespace is private per-user, with consent/break-glass for cross-user access.

## How It Works

### Authentication Flow

```
Request comes in
  │
  ├─ Has Bearer token?
  │    └─ Hash it, look up in principals.token_hash
  │         └─ Found → authenticated as that principal
  │
  ├─ Has session cookie? (dashboard)
  │    └─ Look up session → resolve to principal
  │         └─ Found → authenticated as that principal
  │
  ├─ Path is /health or /dashboard (login page)?
  │    └─ Allow without auth
  │
  └─ No auth
       └─ If ENGRAM_REQUIRE_AUTH=true → 401
       └─ If ENGRAM_REQUIRE_AUTH=false → allow (backwards compat, dev mode)
```

### Write Path

**Agent writing to its namespace** (unchanged):
1. CC sends `POST /memory/set` with its bearer token
2. Auth resolves token → principal `claude-code`
3. Request includes `namespace=claude-code`
4. Server checks: is `claude-code` in this principal's `write_namespaces`? Yes → allow
5. Memory is stored exactly as before

**Human memory via HA** (new routing):
1. User talks to HA voice assistant: "Remember that the kids have soccer practice on Tuesdays"
2. HA pyscript calls engram with `namespace=human`, `user_id=<ha-uuid>`, `channel:ha` in tags
3. Auth resolves HA's bearer token → principal `ha-system` (agent)
4. Server checks: `ha-system` has `human` in its `write_namespaces` (it's a trusted channel)
5. Server resolves `user_id=<ha-uuid>` via alias → principal `ixanadu`
6. Memory stored as `namespace=human, user_id=ixanadu, key=..., tags=channel:ha`

**Human memory via future chat app** (same pattern):
1. User types in chat app: "Remember Sarah's birthday is March 15"
2. Chat app calls engram with `namespace=human`, `user_id=ix@email.com`, `channel:chat` in tags
3. Auth resolves chat app's token → trusted channel
4. Server resolves `user_id=ix@email.com` via alias → principal `ixanadu`
5. Memory stored as `namespace=human, user_id=ixanadu, key=..., tags=channel:chat`

**Key point**: the `user_id` stored in the memory is the principal name (`ixanadu`), not the raw channel identifier. The alias resolution happens at write time, so all of a human's memories are filed under one consistent identity regardless of which channel created them.

### Read Path

**Admin searching from dashboard**:
1. Dashboard sends search with session cookie
2. Auth resolves session → principal `ixanadu` (is_admin=true)
3. For agent namespaces: search fans out across all (or filtered) namespaces
4. For `human` namespace: returns only ixanadu's own memories
5. Results merged, ranked by relevance

**Human searching their own memories**:
1. ixanadu searches "children soccer" from dashboard
2. Search hits `human` namespace filtered to `user_id=ixanadu`
3. Also hits `claude-code` (if ixanadu has read access) in case CC stored related context
4. Merged results from both

**Agent reading another agent's output**:
1. Beast sends `GET /admin/memories?namespace=claude-code` with its token
2. Auth resolves token → principal `beast`
3. `beast` has `read_namespaces = ["beast", "claude-code"]`
4. `claude-code` is in the list → allow
5. Results returned (no human privacy filtering needed — agent namespace)

### Human Privacy Model

Human memories are sacred. This is the hardest part of the design and the most important to get right.

**Core principle**: being an admin grants full access to *agent/system* memories (claude-code, beast, ha, quant-bot, etc.) but does NOT grant access to *other humans'* memories. A human's memories are private to them by default, period.

**Three tiers of memory visibility**:

| Memory type | Who can read | Via what route |
|------------|-------------|----------------|
| Agent/system (claude-code, beast, ha, etc.) | Any principal with that namespace in `read_namespaces` | Any API route or dashboard |
| Your own human memories | You | Any route — dashboard, HA assistant, chat, API |
| Another human's memories | Only them. Not even admin. | See consent/break-glass below |

**How this works technically**: when querying `namespace=human`, the server ALWAYS filters by the requesting principal's `user_id` — even for admins. The only exceptions are consent grants and break-glass.

**Accessing another human's memories — two mechanisms**:

1. **Consent grant**: A human can grant another principal read access to their memories. Stored as a record, revocable anytime, optionally time-limited. Think of it like sharing a photo album — explicit, visible, revocable.

   Example: wife grants ixanadu access so he can help debug her grocery list automation. She can revoke it anytime.

2. **Break-glass**: Admin can access another human's memories without consent, but it requires deliberate action. The dashboard shows a warning: *"You are about to view another person's private memories. This action will be logged and the user will be notified."* Must provide a reason. Recorded in audit log.

   This is the "I could just query the DB directly" escape valve — but it goes through the application layer, gets logged, and creates accountability. The application layer enforces the social contract, not physical impossibility.

## Cross-Namespace Search

New endpoint: `GET /admin/search`

```
GET /admin/search?query=children+soccer&limit=10
Authorization: Bearer <token>  (or session cookie)
```

Server:
1. Resolves principal
2. Determines searchable namespaces:
   - Agent/system namespaces from `read_namespaces` (or all if `*`)
   - `human` namespace, filtered to own `user_id` (+ any consent-granted user_ids)
3. Runs semantic search in each in parallel
4. Merges results by similarity score
5. Returns unified list with namespace and channel in each result

This is what the dashboard search box calls. The existing `/memory/search` stays unchanged (single namespace, used by agents).

## Dashboard Changes

1. **Login page** at `/dashboard` if not authenticated (simple username/password form)
2. **Search box** replaces the key prefix input — calls `/admin/search`, semantic across all readable namespaces
3. **Namespace dropdown** becomes a filter (narrow results), not a requirement. Default: all readable
4. **Namespace selector** no longer shows `human` memories from other users — only your own appear
5. **Break-glass UI**: if an admin navigates to another human's memories, a warning modal with reason field appears before any data loads

## Channel Integration Pattern

Every channel (HA, chat app, email agent, CLI) follows the same pattern when writing human memories:

1. **Authenticate as a trusted channel** — each channel has its own agent principal with `human` in its `write_namespaces`
2. **Include the human identifier** — whatever the channel knows (HA UUID, email, username) goes in `user_id`
3. **Tag the channel** — `tags=channel:ha` or `tags=channel:chat` so the source is traceable
4. **engram resolves the identity** — alias lookup maps the channel-specific ID to the canonical principal name

If a channel sends an unknown `user_id` (no alias match), engram can either:
- Reject (strict mode, recommended when `ENGRAM_REQUIRE_AUTH=true`)
- Store under a provisional identity for an admin to claim later (lenient mode)

### HA-Specific Notes

Home Assistant identifies users via `context.user_id` in automations and conversation agents. The current HA integration passes this through to engram verbatim:

```yaml
user_id: "{{ context.user_id | default('default') }}"
```

After this change, the HA pyscript will need a small update:
- Send `namespace=human` instead of `namespace=ha` for personal memories
- Continue sending `namespace=ha` for system/automation data
- Add `channel:ha` to tags

The HA integration authenticates with its own bearer token (agent principal `ha-system`). The `user_id` in the payload identifies *which human* — resolved via alias.

**Finding HA user IDs**: Admin looks up UUIDs from HA's Settings → People → Users panel, registers them as aliases. One-time per household member.

## Migration / Backwards Compatibility

- **ENGRAM_REQUIRE_AUTH=false** (default initially): everything works as today. No tokens needed. Dashboard has full access. This is dev/single-user mode.
- **ENGRAM_REQUIRE_AUTH=true**: principals table must exist, tokens required. The existing `ENGRAM_API_TOKEN` env var seeds an initial admin principal on first boot.
- **Existing memories in `ha` namespace**: memories already stored as `namespace=ha` will need a one-time migration to move personal memories to `namespace=human` with alias-resolved `user_id`. Similar to the ha_memory → engram migration already completed. System/automation memories stay in `ha`.
- **Existing MCP bridge**: gets its own principal + token. Reads/writes `claude-code` only.
- **Existing HA integration**: gets its own principal + token. Updated to write personal memories to `human` namespace.

## Implementation Order

1. **`principals` + `principal_aliases` + `consent_grants` + `audit_log` tables** — schema, migration SQL in db.py
2. **Principal service** — CRUD for principals/aliases, token hashing, lookup by token/alias
3. **Consent service** — grant, revoke, check consent; break-glass with audit logging
4. **Auth middleware upgrade** — resolve token → principal, attach to request; human privacy filtering on `human` namespace
5. **`/admin/search` endpoint** — cross-namespace semantic search, permission-filtered, human-privacy-aware
6. **Dashboard updates** — login page, semantic search box, namespace as filter not requirement, break-glass UI with warning + reason prompt
7. **Registration tooling** — CLI script or admin endpoint to create principals + tokens
8. **Channel updates** — HA pyscript writes to `human` namespace; MCP bridge gets own token
9. **Migration script** — move personal memories from `ha` → `human` namespace

Steps 1–6 are one coherent change. Steps 7–9 are follow-up.

## Example: Day-One Setup

```
# Create human principals
engram-admin create-principal ixanadu --type human --admin --password ***
engram-admin create-principal wife --type human --password ***

# Create agent principals
engram-admin create-principal claude-code --type agent --read claude-code --write claude-code
engram-admin create-principal beast --type agent --read beast,claude-code --write beast
engram-admin create-principal ha-system --type agent --read ha,human --write ha,human

# Register aliases (how channels identify humans)
engram-admin add-alias ixanadu --alias "<ha-user-uuid>" --source ha
engram-admin add-alias ixanadu --alias "ix@email.com" --source email
engram-admin add-alias wife --alias "<ha-uuid-for-wife>" --source ha

# Enable auth
export ENGRAM_REQUIRE_AUTH=true

# Distribute tokens
# MCP bridge config: ENGRAM_API_TOKEN=<claude-code's generated token>
# HA integration config: ENGRAM_API_TOKEN=<ha-system's generated token>
# Beast config: ENGRAM_API_TOKEN=<beast's generated token>
```

After setup, ixanadu logs into the dashboard and searches "children" — gets results from `human` (their own memories from HA, chat, email), `claude-code` (agent work mentioning children), and any other readable namespace. Wife logs in and searches the same — sees only her own `human` memories plus any agent namespaces she has read access to.

## What This Spec Doesn't Cover (Future)

- **Roles/groups**: "all agents" as a group with shared permissions. Not needed until there are many principals.
- **Fine-grained scope filtering**: "Beast can read `claude-code` but only `shared` scope." Could be added to read_namespaces as `claude-code:shared` syntax later.
- **Notification delivery**: when break-glass access occurs, the spec says "the user will be notified" but doesn't specify how (email, HA notification, dashboard alert). Depends on what notification channels exist.
- **Auto-discovery**: channels sending unknown user_ids trigger provisional principal creation. Useful at scale, overkill for a household.
- **OAuth / SSO**: external identity providers. Way later.