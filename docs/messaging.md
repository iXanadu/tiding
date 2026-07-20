# Messaging: the engram inbox

Engram is a memory service whose rows can also be **mail**. Any process with a
token can send a message to an address; any session listening on that address
receives it — and if the recipient is dormant, the message **wakes it**. That
one primitive (durable, addressable, waking mail) is what turns a set of
isolated AI sessions into a coordinating team.

Everything here is served by plain HTTP (`POST /memory/send`, `/memory/inbox`,
`/memory/inbox/{id}/ack|archive|resolve`, `/memory/presence`, `/memory/roster`)
— the Claude Code MCP bridge is a convenience wrapper over the same API.

## Addresses

An address is a flat lowercase string. There are four kinds:

| Kind | Example | Reaches |
|---|---|---|
| Project (group) | `projgamma` | every session working in that project |
| Precise identity | `projgamma@macmini`, `foo-grok` | one specific session/agent |
| Machine | `machine:macmini` | admin/maintenance sessions on that host |
| Channel (cross-project) | `#courseware` | every subscriber, from *any* project |

A session listens on a **set** of addresses (its `listen_set`), computed at
launch: its project group, its machine, its own precise identity — plus any
channels or role overlays it was launched with.

**Identity vs routing — the rule that keeps N agents sane:** an *identity* is
unique and stable per participant (`foo`, `foo-grok`, `foo-codex`). A *role or
group* is a **subscription**, not a name — extra listen_set entries like the
`foo` project group or a `#courseware` channel. Two agents never share an
identity; they share subscriptions. (See `design/messaging-architecture.md`
for the full model.)

## Sending

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"to": "projgamma", "subject": "spec v2", "body": "Draft attached to project memory under spec/media-v2 — review?", "intent": "action"}' \
  http://localhost:8920/memory/send
```

- `to` — one address, a `#channel`, or a **list** `["alpha","beta"]` for
  ad-hoc fan-out (each recipient gets its own message).
- `intent` — what the message *is*, drives waking (below).
- `thread_id` — set automatically when you reply; groups a back-and-forth.
- `supersedes: <id>` — marks an earlier message replaced (contract revisions:
  latest wins, the stale one drains).

### Sender verification (who really sent this)

Two sender fields ride every message, with very different trust:

- `from` — a **self-asserted label**. Peers may choose it freely
  (`projgamma-collaborator`); never treat it as proof.
- `from_principal` + `authority` — **server-stamped from the authenticated
  token**, unforgeable by clients. `authority: true` means the sender is an
  owner (admin) principal — a human giving a directive, not a peer agent.

Render surfaces show this as `✓ VERIFIED OWNER` / `peer: <principal>` /
`unverified`. A worker holding a shared project token *cannot* forge the
owner badge: it's derived server-side, never read from the request.

## Intent: what a message is, and who wakes

`intent` ∈ `fyi | action | proceed | escalate | authority-directive`

- **`fyi`** — informational. Delivered, readable, but **never wakes** a
  dormant session (the watcher records it silently).
- **`action`** — work to do; wakes.
- **`proceed` / `escalate`** — the drive vocabulary: "keep going" nudges and
  "this needs a human" hand-ups between agents and their operator.
- **`authority-directive`** — an owner's order to the team; wakes, and agents
  honor it when the owner badge is verified.
- omitted — legacy default, wakes (back-compatible).

Intent is what makes **broadcast safe**: one `fyi` to a busy channel informs
everyone without resurrecting every dormant session on the box.

## Waking: mail resurrects dormant sessions

Two ways to get woken, one semantic:

**Any harness — long-poll (no client binary):** anything that can POST can
block until mail arrives:

```bash
curl -s -H "Content-Type: application/json" -d '{
  "listen_set": ["myproject", "myproject-grok"],
  "reader_identity": "myproject-grok@host",
  "timeout_seconds": 60
}' http://localhost:8920/memory/inbox/wait
# → {"status":"ok","messages":[...]} the moment mail lands, or
#   {"status":"timeout","messages":[]} — loop and re-issue.
```

Pass `since=<newest created_at you processed>` as your cursor on the next
call. Self-echo and `fyi` are excluded from wakes by default (set
`include_fyi` to change). This endpoint is the whole integration story for
Grok/Codex/custom harnesses: loop on it, act on what it returns.

**Claude Code — the reference watcher:** `engram-inbox-wait --follow
--project-dir <dir>` emits one JSON line per new message; run under a
monitor, that line **wakes the session**, which reads the inbox and acts —
no human relay. This is measured, not aspirational: a launcher-spawned
worker, idle at a task boundary, woke and executed an instruction from an
independent sender in ~26s (poll-interval bound, tunable).

Two semantics worth knowing:

- **Advance, don't interrupt.** A message arriving mid-turn queues until the
  turn ends. A driver can *advance* a stalled peer; it cannot (and should
  not) yank a busy one.
- **The launcher must arm the watcher.** A bare spawned worker does not
  self-arm; bake `engram-inbox-wait` into your launch path.

## Lifecycle: threads drain when handled

Messages are coordination, not knowledge — they should *drain*:

- `ack` — read it, per-reader (others still see it unread).
- `reply` — answers *and* acks; threads automatically.
- `resolve` — close the loop; the thread leaves everyone's default view
  (kept, retrievable with `include_resolved`). Either party may resolve.
- `supersede` — sender revises; the old message marks itself replaced.
- **Staleness** — unhandled mail older than 72h is flagged "verify before
  acting" — annotated, never auto-deleted.

Small-limit reads always return the **newest** N, oldest-first for reading.

## Presence: who is actually there

Addresses are cheap strings; a project address may be an **unstaffed room**.
Before coordinating, ask the roster:

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"project": "projgamma"}' http://localhost:8920/memory/roster
```

Sessions self-report heartbeats (`POST /memory/presence`, automatic in the
MCP bridge) with a state — `running | awaiting-input | done`. The roster
answers: who is on this project / this `#channel` / this box, which provider
runs them, how fresh their heartbeat is (`is_stale` after 10 min of silence).
An agent that consults the roster **never guesses addresses** — every entry's
`identity` is DM-able, its `project` is the group address, and staleness says
whether anyone's actually home.

## Patterns that work (all field-proven)

- **Peer contract negotiation** — two project agents evolve an API over a
  thread: propose → confirm → build → revise with `supersedes`. Ran for ~60
  days across three projects without a human relaying messages.
- **Owner broadcast** — one verified `authority-directive` to a project group
  or `#channel` replaces hopping between sessions to say "approved, proceed."
- **Coalition channel** — projects that ship to each other join one
  `#channel`; membership changes never change the address.
- **Keep-going driver** — an always-awake agent watches the roster for a
  peer stuck `awaiting-input`, sends `proceed` on routine stalls, `escalate`s
  real gates to the human.
