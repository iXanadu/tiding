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
  .engram.cfg                  # project = myproject  ← the ONLY engram line a repo carries
  AGENTS.md                    # provider-neutral project instructions (canonical)
  CLAUDE.md -> AGENTS.md       # symlink: Claude Code reads the same file
  skills/                      # provider-neutral startup/wrapup/init workflows (canonical)
  .claude/skills -> ../skills  # symlink: Claude's skill path resolves into it
  BACKLOG.md                   # lean open-items ledger (see backlog-standard.md)
```

One canonical artifact per concern; every provider-specific path is a symlink
into it. Nothing is duplicated, so nothing can drift.

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

# 2. Point the agent at the same server (MCP bridge env block, or your
#    client's settings). Do NOT set memory_namespace — the namespace is
#    decided by the token, server-side (see design/provider-credentials.md):
#      memory_api_url   = http://localhost:8920
#      memory_api_token = <grok's token>
#      ENGRAM_INBOX_IDENTITY = <project>-grok        # if sharing a folder

# 3. Verify identity end-to-end
curl -s -H "Authorization: Bearer $GROK_TOKEN" http://localhost:8920/whoami
```

First-run smoke test: have the new agent `memory_store` a note, `memory_search`
it back, send one message to a peer, and call `memory_roster` to see itself
listed. All four worked on the first live Grok onboarding — the only stumble
was addressing (it guessed an address instead of asking the roster; the
roster exists precisely so no agent has to guess).

## A second session in the same folder (seats)

Two sessions in one project folder — same provider or not — MUST take
distinct seats, declared **at launch**:

```bash
ENGRAM_INBOX_IDENTITY=myproject-remediate claude   # seat 1
ENGRAM_INBOX_IDENTITY=myproject-audit     grok     # seat 2
```

Both keep the shared `myproject` group address (broadcasts reach everyone);
each gets its own DM address and independent read-state. Discriminate by
**role** first (`-audit`, `-remediate`), by provider/model only when that is
the real distinction. The watcher inherits the same env, so bridge and
watcher always agree.

Set `ENGRAM_PROVIDER=<claude|grok|…>` alongside the seat. The bridge is one
provider-neutral module that every harness spawns, so it cannot tell from the
inside who launched it — unset means the roster reports `claude` (the
historical default), and two providers become indistinguishable exactly when
you most need to tell them apart.

> **Never pin these in a provider's static MCP `env` block.** Launch-time
> injection *is* the mechanism. Where a provider's config declares a static env
> map for the engram server (Grok's `[mcp_servers.engram.env]`), **the config
> block wins over the parent environment** — so a seat pinned there silently
> defeats every per-launch override, with no error and no banner. Grok already
> pins `ENGRAM_IDENTITY` (the *credential selector*) there, which is correct
> and must stay; seats and channels must not join it. Claude Code has no such
> block, so this asymmetry is Grok-specific. Verified by controlled probe
> 2026-07-23: Grok inherits the full parent environment and merges the config
> block **on top**.

**If you forget, engram tells you.** Every session heartbeats a per-process
nonce; when the server sees two live sessions on one identity it flags a
**seat collision** — a ⛔ STOP banner on the colliding sessions' memory
calls and on the roster — because two sessions on one seat share ack-state
and cannot message or wake each other. Deliberately shared roles (the
`admin` identity) are exempt. Fix = relaunch one session with a seat, and
arm its watcher with the same env.

**Arm the watcher by inheritance, not by discipline.** Spawn it as a *child of
the agent process* so it inherits that process's environment. Then there is no
second place a seat is chosen and no separate step a future edit can drop. A
bridge that is seated while its watcher is not is the worst state available:
the roster shows the session correctly seated and it silently never wakes —
a failure with no symptom.

## Collaboration topologies (both live-proven)

**Peer mesh** — each agent owns a part, coordinating through threaded
contract negotiation. The reference case: three projects (course authoring →
media generation → learner delivery) ran ~60 days as three independent
"senior engineers," growing their APIs through engram threads, human
approvals only at real gates.

**Driver + worker** — one agent keeps another moving. The worker stalls at a
task boundary; the driver sees it in the roster (`awaiting-input`), sends
`proceed` (routine) or `escalate`s to the human (irreversible/costly). The
wake mechanics are validated: a spawned worker at a seam, woken by an
independent sender's message, acted autonomously in ~26s. Two hard rules
learned from that test: the **launcher must arm the inbox watcher** (workers
don't self-arm), and drivers **advance stalled peers — mid-turn messages
queue** rather than interrupt.

**Either provider can take either seat.** Every agent session is dormant
between turns — that is not a Claude trait, and no supported provider polls
engram on its own. What wakes a dormant session is the same primitive
everywhere: a **harness-level background process** whose stdout re-enters the
session as a turn (Claude Code's Monitor tool, Grok's `monitor` tool), holding
`engram-inbox-wait --follow` open for the session's lifetime. The engram MCP
tools are turn-scoped and cannot do this — the watcher is deliberately a plain
shell process for exactly that reason.

> Corrected 2026-07-23. This page previously described Grok as "always-awake…
> doesn't pause" and built the driver/worker split on it. That was wrong about
> the mechanism, and it was steering topology decisions. Dormancy is universal;
> the real variable is **whether a given session armed its watcher**, which is
> equally true of every provider. Choose driver vs worker on the work, not the
> vendor.

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
