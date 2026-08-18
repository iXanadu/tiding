# Getting started: identity, scoping, and security

**The whole adoption path, in order:**

1. **Install the server** — README [Quick Start](../README.md#quick-start)
   (or as a boot service: [deployment.md](deployment.md) — spoiler: it runs
   from any directory).
2. **First memory + first message** — the five-minute roundtrips below.
3. **Wire your agent(s)** — one command per provider:
   [multi-provider.md](multi-provider.md#the-wiring-playbook-per-box-per-provider)
   (`scripts/wire-provider.sh`).
4. **First project** — `.engram.cfg` + scoping, below.
5. **Make agents talk** — [messaging.md](messaging.md), then build the fun
   thing: [a group-chat huddle](build-a-huddle.md) between you and N agents.

The two things that decide whether your first hour with engram is smooth are
**identity** (who is writing, where does it land) and **security posture**
(who can reach your server). Both are small; both bite if skipped.

## First five minutes

```bash
# 1. Run the server (see README Quick Start for setup)
uvicorn server.main:app --port 8920

# 2. Store a memory, then find it by MEANING (not the key) — the memory "aha"
curl -s -H "Content-Type: application/json" \
  -d '{"namespace":"main","key":"hello","value":"engram is up","scope":"machine"}' \
  http://localhost:8920/memory/set
curl -s -H "Content-Type: application/json" \
  -d '{"namespace":"main","query":"is engram up?","scope":"machine"}' http://localhost:8920/memory/search

# 3. Send a message from one identity, read it as another — the coordination "aha"
curl -s -H "Content-Type: application/json" \
  -d '{"to":"myproject","from_":"me@laptop","subject":"hi","body":"first mail"}' \
  http://localhost:8920/memory/send
curl -s -H "Content-Type: application/json" \
  -d '{"listen_set":["myproject"],"reader_identity":"myproject@laptop"}' \
  http://localhost:8920/memory/inbox
```

Two roundtrips, two halves of engram. **Store → search** is durable memory an
agent recovers across sessions — you asked "is engram up?" and got back a note
keyed `hello`, matched by meaning. **Send → receive** is one identity reaching
another — the coordination model in miniature. Everything else refines these two.

## ⚠️ SECURITY POSTURE — read this before exposing anything

An engram reachable on a network **is your agents' entire memory and message
bus**. Unauthenticated + reachable = anyone can read it, poison it, or
impersonate your agents. Engram is **secure by default** and enforces this:

| Posture | Config | Verdict |
|---|---|---|
| Loopback, no auth | `ENGRAM_HOST=127.0.0.1` (default) | ✅ Fine — nothing external can reach it |
| Private/overlay net (Tailscale, WireGuard), no auth | `ENGRAM_HOST=0.0.0.0` + `ENGRAM_ALLOW_INSECURE_BIND=true` | ⚠️ Acceptable **only** if the interface is genuinely private — you are opting out of auth **explicitly** |
| Reachable, with auth | `ENGRAM_HOST=0.0.0.0` + `ENGRAM_REQUIRE_AUTH=true` (+ principal tokens) | ✅ The intended networked posture |
| Reachable, no auth, no opt-out | — | 🛑 **The server refuses to start.** This is deliberate. |

Rules of thumb:

- **Never** put a tokenless engram on a public interface. The startup guard
  will stop you; do not "fix" that by setting the opt-out on a public box.
- The opt-out (`ENGRAM_ALLOW_INSECURE_BIND=true`) exists for one case:
  a personal fleet on a private overlay network. If you're not sure your
  network qualifies, it doesn't — use auth.
- Turning on auth: set `ENGRAM_REQUIRE_AUTH=true` and `ENGRAM_API_TOKEN`
  (bootstraps an admin), then mint per-agent principal tokens
  (`POST /admin/principals`) and retire the bootstrap. Tokens are shown once
  and stored hashed.
- Tokens live in config outside the repo (`~/.config/…`, env). **Never
  commit a token.**

## Identity: the partition dimensions

Every memory is scoped by independent dimensions:

- **namespace** — which *system* is writing (required, no default). One
  shared namespace for your cooperating agents is the norm; separate
  namespaces are for genuinely separate systems, not for separate providers
  working the same projects. The namespace is the **security boundary** —
  access control happens here.
- **scope** — visibility: `shared` (cross-project knowledge), `project`
  (this project's state), `machine` (host-local), `user` (personal).
- **user_id** — the *person or principal* that wrote the memory (for
  `scope=machine` it's the hostname).
- **project** — a separate column holding the project name when
  `scope=project` (NULL otherwise). The project name comes from
  `.engram.cfg`, never from a folder basename guess.

(Older docs described a 3-dimension model where `user_id` doubled as the
project name for `scope=project` — that was Phase 3. Phase 4 split `project`
into its own column so the writer's identity survives as provenance.)

## `.engram.cfg`: the project's one config file

A `scope=project` memory must land in the *same bucket* no matter which
checkout, machine, or provider wrote it. Path basenames can't guarantee that
(`~/projects/foo` vs `/var/www/foo/prod`), so the project declares its name
in a git-tracked file at the repo root:

```ini
# .engram.cfg
project = foo
```

- Clients resolve it by walking up from the working directory; the session's
  spawn directory anchors resolution when a call omits the path.
- **No real cfg → clients must ask, not guess.** Deploy labels (`prod`,
  `dev`, `main`…) are treated as unset — a placeholder never becomes an
  identity silently.
- The folder carries **nothing else**: no tokens and no per-agent identity.
  Those are injected at launch (see
  [multi-provider.md](multi-provider.md)) — which is exactly what lets
  several agents share one checkout cleanly. (The one addressing thing a
  folder MAY declare is team `groups =` — see
  [messaging.md](messaging.md) — because a team address is bound to the
  codebase, not to a session.)
- **Two agents in one folder just work.** Each session claims a distinct
  address from the server at startup — `foo-claude`, `foo-claude-2`,
  `foo-grok` — while all keep the shared `foo` group address. No manual
  assignment, no way to collide. See
  [multi-provider.md](multi-provider.md#a-second-session-in-the-same-folder-seats).
- Optional second line `inbox_identity = foo-server` gives a *folder* a
  distinct default mail address — for the one-identity-per-folder layout (e.g.
  `foo/` and `foo-app/` as sibling repos of one project). It seeds the
  preference; the server still guarantees uniqueness.

## Where to next

- [daily-workflow.md](daily-workflow.md) — how a human + agents actually
  live with a memory-first workflow (startup recall, milestone stores,
  handoffs, the team loop).
- [messaging.md](messaging.md) — addresses, intent, waking, lifecycle,
  presence.
- [multi-provider.md](multi-provider.md) — adding Grok/Codex/anything with
  a token.
