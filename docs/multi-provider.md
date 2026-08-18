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

3. **Its own inbox identity — allocated for you.** Two agents in one project
   folder need distinct addresses to DM each other and to keep read-receipts
   from colliding. The bridge **claims** one from the server at startup, so
   this is automatic: `foo` (Claude) and `foo-grok`, or `foo-claude` and
   `foo-claude-2` for two of the same provider. The project's `.engram.cfg`
   stays identity-free (`project = <name>` only). Every seat keeps the shared
   `foo` group address in its listen_set, so broadcasts still reach both
   agents. A launcher may *prefer* a seat (`ENGRAM_INBOX_IDENTITY=foo-audit`,
   granted when free); a raw-HTTP client passes `reader_identity=` / `from_=`
   per call. Full mechanics in [the seats section below](#a-second-session-in-the-same-folder-seats).

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

**If you run one agent per folder, nothing here changes and nothing is
required of you.** Your session's address stays the plain project name
(`myproject`), exactly as before. Read on only when a second agent joins.

Put two agent sessions in one project folder — same provider or not — and each
**claims a distinct address from the server automatically**. No manual
assignment, and no way for the two to collide. The first session keeps
`myproject`; the second is granted `myproject-claude` (or `myproject-grok`),
and a third `myproject-claude-2`. All of them keep the shared `myproject`
group address (broadcasts reach everyone); each also gets its own DM address
and independent read-state.

The rule is simply *first come keeps what it asked for*: a session requests
the address it would have used anyway, and the server only hands out something
different when that one is already taken. So the feature costs nothing until
the moment you actually need it.

This is the **session registry**. Addressing is exactly two layers:

| Layer | Example | Reaches |
|---|---|---|
| Project **group** | `myproject` | every session in the folder |
| **Seat** | `myproject-claude-2` | exactly one session |

A **role** (tester, orchestrator, implementer) is deliberately *not* a third
layer. A role isn't unique — ask both Claude and Grok to test and
`myproject-tester` would name two sessions, the exact collision seats exist to
prevent — and it's assigned late, to a session you already picked. You attach
roles when you convene the work (in a [huddle](build-a-huddle.md)), addressing
the specific seats involved; the address itself stays
`<project>-<provider>[-ordinal]`.

### You don't assign seats — you can prefer one

The bridge claims a seat on startup, so the common case needs nothing from you.
A launcher may still state a **preference** — the server grants it when free
and hands out the next ordinal when a peer already holds it:

```bash
# a preference, not an assignment — honored when free, ordinal-suffixed when taken:
ENGRAM_INBOX_IDENTITY=myproject-audit ENGRAM_PROVIDER=grok grok
```

So the old manual-seat line still works; it just can't collide any more. If two
launchers (or a launcher and a hand-started terminal) both ask for
`myproject-audit`, one gets it and the other gets `myproject-audit-2` — the
server is the only party that sees every session, so it is the only place that
can guarantee uniqueness.

### Reading the granted seat (launchers and UIs)

Because the granted seat can be an ordinal the launcher didn't predict, **read
it back — never recompute it**:

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"session_key":"<the key you injected>"}' http://localhost:8920/session/seats
# → {"seats":[{"seat":"myproject-audit-2","provider":"grok","session_key":"…"}]}
```

Key any UI, badge, or router on the returned **seat**, and take provider from
the **field** — never parse either off the address string (a `-2` tail is an
ordinal, a `-grok` tail is a provider, and nothing in the string reliably tells
them apart). A picker must **show the ordinal**: `myproject-claude` and
`myproject-claude-2` are different sessions, and hiding the suffix collapses
them to identical rows. Middle-truncate address labels — the ordinal is the
tail, and default truncation (CSS `text-overflow: ellipsis`, SwiftUI) drops the
tail first.

### The launcher's job (unchanged, plus one key)

Set `ENGRAM_PROVIDER=<claude|grok|…>` alongside the seat. The bridge is one
provider-neutral module that every harness spawns, so it cannot tell from the
inside who launched it — unset means the roster reports `claude` (the
historical default), and two providers become indistinguishable exactly when
you most need to tell them apart.

Set `ENGRAM_SESSION_KEY` to a value that is **stable across a respawn and never
pid-derived** (a launcher's own session name is ideal). The seat is keyed to it,
so a bridge restart re-claims the *same* seat instead of drifting to a new
ordinal. A **hand-launched** session — no launcher, no injected key — still
self-allocates: the bridge derives a key from its own harness process, so
co-working works even when you decide *after* launch that two agents should
pair.

> **Never pin these in a provider's static MCP `env` block.** Launch-time
> injection *is* the mechanism. Where a provider's config declares a static env
> map for the engram server (Grok's `[mcp_servers.engram.env]`), **the config
> block wins over the parent environment** — so a seat pinned there silently
> defeats every per-launch override, with no error and no banner. Grok already
> pins `ENGRAM_IDENTITY` (the *credential selector*) there, which is correct
> and must stay; seats must not join it. Claude Code has no such
> block. Verified by controlled probe 2026-07-23: Grok inherits the full parent
> environment and merges the config block **on top**.
>
> **Cursor is stricter, and the rule inverts there — see the Cursor section
> below.** It passes an MCP server *only* what its config block lists, so a
> launch-injected value never arrives at all. Under Cursor, pinning is not a
> hazard to avoid but the only delivery channel that exists.

**The safety net — collision detection.** Auto-allocation prevents collisions
for any session that claims a seat, which is the normal path. Two kinds of
session still can't claim, and the older guard covers them: a session started
*before* the registry existed, and one whose engram was unreachable at startup
(it keeps its locally-computed seat and retries). For those, every session
heartbeats a per-process nonce, and when the server sees two live sessions on
one identity it flags a **seat collision** — a ⛔ STOP banner on the colliding
sessions' memory calls and on the roster — because two sessions on one seat
share ack-state and cannot message or wake each other. Deliberately shared
roles (the `admin` identity) are exempt. Fix = relaunch one session so it
claims, and arm its watcher with the same env. In normal operation you should
never see this banner; if you do, a session is running that predates the
registry or couldn't reach it.

**Arm the watcher by inheritance, not by discipline.** Spawn it as a *child of
the agent process* so it inherits that process's environment. Then there is no
second place a seat is chosen and no separate step a future edit can drop. A
bridge that is seated while its watcher is not is the worst state available:
the roster shows the session correctly seated and it silently never wakes —
a failure with no symptom.

**Re-seating mid-session.** A session that learns it is co-working can take a
seat at runtime with `memory_take_seat` — useful when nothing seated it at
launch. That moves the *bridge* instantly, but the watcher is a separate
process that resolved its identity at start, which would leave exactly the
split state above.

When the launcher injects `ENGRAM_SESSION_KEY` (a per-session value, stable
across respawn, never pid-derived), the seat is also recorded to a file keyed
on it, and **the watcher re-reads that file every poll** — so a re-seat
propagates within one poll interval with no restart and no re-arm step. The
split state becomes impossible rather than documented.

Without a session key there is no file, both sides fall back to start-time
resolution, and `memory_take_seat` says plainly that you must re-arm the
watcher yourself. The seat file is read defensively: missing, unreadable or
malformed all resolve to "no file" rather than an error, because a watcher
listening on a *stale* seat still catches project-addressed mail, while a dead
one catches nothing.

## Wiring Cursor (`cursor-agent`) — and the two rules that differ

Cursor onboards like any other provider — mint a principal, point it at the
server — but it breaks two assumptions the rest of this page is built on, both
verified by controlled probe 2026-08-09. Read these before wiring it.

```bash
scripts/wire-provider.sh cursor --kind http --admin-token "$ADMIN_TOKEN" \
  --read "fleet,claude-web,grok,beast" --write "fleet"
```

⛔ **Do NOT put an engram entry in the GLOBAL `~/.cursor/mcp.json` if anything
also spawns Cursor sessions with their own engram server** (a driver over ACP
does exactly that). Cursor spawns **both** and routes by NAME, so two servers
called `engram` means the global silently wins and the session reads mail at
the wrong address while every surface advertises its per-session seat. Measured
2026-08-10; it cost a live session. The same applies to a per-project
`.cursor/mcp.json`, which loads **alongside** the global rather than replacing
it — so a repo that grows one later reintroduces the collision, and it fails at
the next *spawn* rather than at the edit.

Use the block below only where NOTHING else injects an engram server — a box
with hand-launched Cursor only. Where a driver is in play, the driver's
per-session block is the whole configuration and this file should not exist:

```json
{ "mcpServers": { "engram": {
    "type": "stdio",
    "command": "<venv>/bin/python",
    "args": ["-m", "engram_mcp.server"],
    "env": { "ENGRAM_IDENTITY": "cursor", "ENGRAM_PROVIDER": "cursor" }
} } }
```

**1. The config `env` block is the ONLY channel to the bridge.** Cursor does not
pass the parent environment through at all — not "config wins", but "parent
never arrives". Probe: with `ENGRAM_INBOX_IDENTITY` exported in the parent, the
session listened on its default address; with the identical variable in the
config block, it listened on the injected one. A third run with `env` omitted
delivered nothing.

Consequences, in order of how easily they bite:

- **`ENGRAM_IDENTITY` must be in the block.** Without it the bridge falls back to
  the default identity file — so Cursor would authenticate as *another
  provider's principal*, silently. That breaks "share the bucket, never the
  identity" with no error.
- **`ENGRAM_PROVIDER` belongs there too.** It does not vary per launch (a
  `cursor-agent` session is always `cursor`), so it is the same safe category as
  the credential selector — unlike seats.
- **A launcher cannot prefer a seat.** `ENGRAM_INBOX_IDENTITY` in the parent
  environment is simply lost. Note what this does *not* mean: sessions still
  **auto-allocate** seats server-side (`myproject-cursor`, then `-2`), so
  co-working works and addresses never collide. What you lose is *choosing* the
  name — role seats such as `myproject-audit`. A driver that needs a specific
  seat must write it into the config block; it must NOT rewrite the shared
  global file per spawn (concurrent launches race, and see the collision
  warning above).
  ⚠️ **And a seat in that block is still only a PREFERENCE.** The server grants
  it when free and ordinal-suffixes it when taken, so a driver that advertises
  the value it injected can be naming a *different session*. Measured
  2026-08-10: a live session injected `proj-cursor` and was granted
  `proj-cursor-7`, while `proj-cursor` belonged to an older session — mail to
  the advertised address would have reached the wrong one, the same symptom as
  the collision above with the collision removed. **Read the granted seat back
  from `POST /session/seats` keyed on the `session_key` you injected, and
  render that** — see [Reading the granted seat](#reading-the-granted-seat-launchers-and-uis).

**2. There is no in-session watcher, so the wake path lives outside Cursor.**
Cursor has no blocking-stream equivalent to Claude Code's Monitor or Grok's
`monitor` — its hooks and `/loop` fire on turn boundaries or a timer, and none
of them is an external event entering a dormant session. Under ACP, though, the
**client owns turn initiation**: a driver holding `engram-inbox-wait --follow`
can issue `session/prompt` when mail lands. So a Cursor seat is a full peer
*when something drives it over ACP*, and a hand-launched terminal Cursor session
has no wake path at all — it can be addressed, and will read its mail whenever
it is next prompted.

**Global agent rules.** Cursor reads `~/.cursor/AGENTS.md`; symlink it to the
canonical file the way the other providers do, so one edit reaches every
harness:

```bash
ln -s ~/.agents/AGENTS.md ~/.cursor/AGENTS.md
```

**Model reporting.** Cursor is *1-to-many* — one session can run several
vendors' models and `session/set_model` succeeds mid-session — and it records no
model in its session files. The bridge therefore reads its current selection
from `~/.cursor/cli-config.json` and reports it as `harness-config`, a source
deliberately named apart from `transcript`: that file is a global selection, so
it is right for one session and stale for any other running concurrently, and it
does not follow an ACP `set_model`. **A driver that sets the model per session
should pass `ENGRAM_MODEL` in the config block** (reported as `declared`, and it
outranks the config read) — and must re-set it when it changes the model, or it
will assert a model the session has left behind, which is worse than reporting
nothing.

Whatever the source, it is stored on the memory row it wrote, as
`metadata.model` alongside `metadata.model_source`. The source is recorded
**even when the model is unknown**, so a reader can always tell "this harness
records nothing" from "nobody looked". Messages do not carry it yet.

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
token can forge** — so "everyone on `courseware`: approved, proceed" lands
with verified weight on every session in that project, from one send (a
cross-project coalition is a fan-out list). See
[messaging.md](messaging.md) for intent and verification details.

## Boundaries that keep it clean

- Tokens live in provider-global config (`~/.config/…`, launcher env) —
  **never in the repo**. The folder carries only `project = <name>`.
- The folder carries **no session addressing** — identity and role overlays
  are injected at launch, so N providers can share one checkout without
  stepping on each other. (Team `groups =` in `.engram.cfg` is the deliberate
  exception: a team address binds to the codebase, not a session.)
- One provider = one principal. One project = one memory bucket. Identity
  discriminates; access does not.
