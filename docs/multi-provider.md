# Multi-provider: Claude + Grok + Codex on one engram

Engram is provider-neutral by construction: it speaks plain HTTP with Bearer
tokens, so **any** agent — Claude Code via the bundled MCP bridge, Grok or
Codex via the same bridge or a raw HTTP client, your own harness — can share
one memory and one message bus. Two agents from different vendors read and
write the **same durable project memory** — each sees what the other decided
and picks up where the other left off — and message each other on top of it.
That shared memory, plus messaging, is what lets a Claude session and a Grok
session **work the same project as one team**.

The rules that make it safe come down to one sentence:

> **Share the bucket, never the identity.**

## The three things every provider agent gets

1. **Its own principal + token.** Mint one per provider
   (`grok`, `codex`, …) with the same namespace grants as your existing
   agent principal. Never let a second provider borrow the first one's
   token — every write and message is attributed to the authenticated
   principal (`from_principal`, audit log), and shared tokens destroy that
   provenance permanently.

2. **The same memory bucket.** Point the new agent at the *same* namespace
   and project scoping your existing agents use. Two providers co-developing
   in one project must read and write **one** project memory — never give a
   provider "its own namespace" for shared project work, or you split the
   project's brain in half. Provider identity is *provenance metadata*, not
   a partition axis.

3. **Its own inbox identity.** Two agents in one project folder need
   distinct addresses to DM each other and to keep read-receipts from
   colliding. The project's `.engram.cfg` stays identity-free
   (`project = <name>` only); per-agent identity is injected **at launch**:
   - MCP bridge: `ENGRAM_INBOX_IDENTITY=foo-grok` in that agent's env
   - Raw HTTP: pass `reader_identity=` / `from_=` per call

   A declared identity keeps the shared project group in its listen_set —
   so `foo` broadcasts still reach both agents, while `foo` (Claude) and
   `foo-grok` have private addresses. Discriminate by whatever is unique in
   *your* seat map: role (`-server`/`-app`), provider (`-grok`/`-codex`),
   or provider+ordinal (`-claude-2`).

## The wiring playbook (per box, per provider)

One command does the exacting parts — principal, identity file, verification —
and prints the exact registration snippet for your provider's config:

```bash
# new provider, mint a principal (admin token from your secret store):
scripts/wire-provider.sh gpt --kind http --admin-token "$ADMIN_TOKEN"
# existing token:
scripts/wire-provider.sh grok --kind grok --token engram_xxx
```

The shared plumbing (the bridge package in its venv) is installed once by
`scripts/install.sh`; every provider on the box reuses it. Per provider, the
whole wiring is: **one identity file + one selector line + one paste** —
see [design/provider-credentials.md](design/provider-credentials.md) for the
resolution rules.

**Project-side, the reference layout is provider-neutral** (a live example
pattern):

```
myproject/
  .engram.cfg            # project = myproject   ← the ONLY engram line a repo carries
  AGENTS.md              # provider-neutral project instructions
  CLAUDE.md -> AGENTS.md # symlink so Claude Code reads the same file
  skills/                # provider-neutral startup/wrapup/init workflows
  BACKLOG.md             # lean open-items ledger (see backlog-standard.md)
```

Globally the same trick applies: keep one canonical agent-rules file (e.g.
`~/.agents/AGENTS.md`) and symlink/import it from each provider's global
config — provider files carry only provider-specific mechanics.

## Wiring a Grok / Codex agent (manual checklist)

```bash
# 1. Mint the principal (as an admin) — mirror your main agent's grants
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"grok","type":"agent","read_namespaces":["fleet","grok"],"write_namespaces":["fleet"]}' \
  http://localhost:8920/admin/principals
# → raw token shown ONCE; store it in the provider's config (never the repo)

# 2. Point the agent at the same server / namespace / project scoping
#    (MCP bridge env block, or your client's settings):
#      memory_api_url   = http://localhost:8920
#      memory_api_token = <grok's token>
#      memory_namespace = <your shared namespace>
#      ENGRAM_INBOX_IDENTITY = <project>-grok        # if sharing a folder

# 3. Verify identity end-to-end
curl -s -H "Authorization: Bearer $GROK_TOKEN" http://localhost:8920/whoami
```

First-run smoke test: have the new agent `memory_store` a note, `memory_search`
it back, send one message to a peer, and call `memory_roster` to see itself
listed. All four worked on the first live Grok onboarding — the only stumble
was addressing (it guessed an address instead of asking the roster; the
roster exists precisely so no agent has to guess).

## Collaboration topologies (both live-proven)

**Peer mesh** — each agent owns a part, coordinating through threaded
contract negotiation. The reference case: three projects (course authoring →
media generation → learner delivery) ran ~60 days as three independent
"senior engineers," growing their APIs through engram threads, human
approvals only at real gates.

**Driver + worker** — one always-awake agent (in practice: Grok, which
doesn't pause) keeps a builder moving. The worker stalls at a task boundary;
the driver sees it in the roster (`awaiting-input`), sends `proceed`
(routine) or `escalate`s to the human (irreversible/costly). The wake
mechanics are validated: a spawned worker at a seam, woken by an independent
sender's message, acted autonomously in ~26s. Two hard rules learned from
that test: the **launcher must arm the inbox watcher** (workers don't
self-arm), and drivers **advance stalled peers — mid-turn messages queue**
rather than interrupt.

## The human in the mesh

The owner is a first-class participant, not an outsider. An owner (admin)
principal's messages carry a server-stamped `authority: true` that **no agent
token can forge** — so "everyone on `#courseware`: approved, proceed" lands
with verified weight on every agent across every project in the coalition,
from one send. See [messaging.md](messaging.md) for intent and verification
details.

## Boundaries that keep it clean

- Tokens live in provider-global config (`~/.config/…`, launcher env) —
  **never in the repo**. The folder carries only `project = <name>`.
- The folder also carries **zero addressing** — identity, role overlays, and
  channel membership are all injected at launch, so N providers can share
  one checkout without stepping on each other.
- One provider = one principal. One project = one memory bucket. Identity
  discriminates; access does not.
