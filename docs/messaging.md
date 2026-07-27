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
identity; they share subscriptions.

### Seats: the server allocates identities, sessions don't invent them

That rule used to be advice. It is now enforced: a session **claims** its
address and engram hands back one nobody else holds.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"session_key":"claude-ab-projgamma","project":"projgamma","provider":"claude"}' \
  http://localhost:8920/session/claim
# → {"seat": "projgamma-claude-2", ...}
```

Three Claude sessions in one folder get `projgamma-claude`,
`projgamma-claude-2`, `projgamma-claude-3` — all still listening on the
`projgamma` group, so broadcasts reach every one of them. Before this they all
computed the same name, shared ack-state, and **could not wake each other**.

Addressing is exactly two layers: the project **group** (everyone) and the
unique provider-discriminated **seat** (exactly one session). There is no
third. A role — tester, orchestrator, implementer — is **not** an address: it
is not unique (you might ask both grok and claude to test) and not
provider-stable, so a role-as-address reintroduces the very collision seats
kill. Roles are how you describe *why* you want several agents; you assign them
in the **huddle** to whichever seats you picked, and the huddle thread carries
that conversation. The seat stays pure plumbing.

- **`session_key`** is stable per session (a launcher's, or derived from the
  harness process). Re-claiming with it returns the *same* seat, so a bridge
  restart never moves a running session's address.
- **`preferred_seat`** is a request, not an assignment — granted when free.
- **`runtime_seat: true`** marks the preferred seat as a *deliberate
  mid-session choice* (`memory_take_seat`): the registry **moves** the
  registration to it instead of answering with the seat it already holds, so
  continuity returns the seat the session is actually on — including across a
  restart. If the name is held by another live session the claim refuses
  loudly (granted seat + warning) and the client reverts to the granted seat:
  both outcomes are consistent, neither is silent.
- **`POST /session/release`** frees a seat immediately; otherwise it is
  reclaimed after a grace period — but **never** while undelivered mail is
  addressed to it.

**Launchers: read the granted seat, don't reconstruct it.** A launcher never
calls `/session/claim` — the bridge inside the session does — so it cannot see
the grant. Ask instead:

```bash
curl -s ... -d '{"session_key":"claude-ab-projgamma"}' \
  http://localhost:8920/session/seats
# → {"seats":[{"seat":"projgamma-claude-2","aliases":["projgamma-auditor"], ...}]}
```

Anything keyed on a session's address (a UI badge map, a router) must key on
the **seat** read from here, and take provider as the **field** it returns —
never parse it off the string. Recomputing `<project>-<provider>` locally is a
guess that misses silently the moment an ordinal is granted, and a `-2` tail is
only an ordinal by coincidence. Read the truth; don't infer it.

A launcher's UI should **show the ordinal** when present: `projgamma-claude`
and `projgamma-claude-2` are different sessions, and a picker that renders both
as "projgamma (claude)" hides the only thing that tells them apart.

Watch the truncation default. The ordinal is the **tail** of the address, and
both `text-overflow: ellipsis` in CSS and SwiftUI's default truncate at the
tail — so on a narrow row or a long project name the one character that
distinguishes two sessions is the first thing dropped, collapsing them into
identical-looking rows through the renderer rather than through the address.
Middle-truncate any address label so the tail survives.

The MCP bridge does all of this automatically. Design and rationale:
[docs/design/session-registry.md](design/session-registry.md).

### Is anyone listening? (`watcher_alive`)

A session is dormant between turns. It only learns that mail arrived because a
**watcher** (`engram-inbox-wait`) is running beside it and wakes it. A session
that never armed one is fully addressable and *permanently silent*: mail is
accepted, stored, and read by nobody until a human types into that terminal.

That state used to be invisible. "Nobody is listening at this address" looked
exactly like "not read yet." The roster now reports it:

```bash
curl -s ... -d '{"project":"projgamma"}' http://localhost:8920/memory/roster
# → {"entries":[{"identity":"projgamma-claude", "state":"running",
#                "watcher_alive": true, "watcher_last_seen":"2026-07-25T22:40:11Z", ...}]}
```

`watcher_alive` is **three-valued, and the third value matters**:

| value | meaning |
|-------|---------|
| `true` | a watcher beat recently — mail will wake this session |
| `false` | a watcher used to beat here and has stopped — **addressable but deaf** |
| `null` | no watcher has ever beaten here: **no basis**, not a "no" |

**Never coerce `null` to `false`.** Absent is not dead. That conflation is what
once let a live session's address be handed to somebody else.

Two rules for consumers:

- **`watcher_alive: false` is not a reclaim signal.** A session can be doing
  real work with a dead watcher. It is unreachable, not gone — "don't expect a
  reply", never "take the address."
- **`state` and `watcher_alive` are independent.** The pair worth acting on is
  `state: running` with `watcher_alive: false`: running, addressable, and deaf.

The watcher beat also fixes a subtler problem. The ordinary heartbeat rides
tool calls, so it measures *activity* — a session heads-down on a long build
stops beating and ages toward reclaimable while it is alive and listening. No
timeout can tell quiet from dead; a bigger number is not a different kind of
answer. The watcher polls on its own timer and lives exactly as long as the
session, so it measures *existence*, and its beat refreshes both the presence
row and the seat.

Arm one at session start and leave it running:

```bash
engram-inbox-wait --follow --project-dir /path/to/repo
```

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

### Sending to someone who isn't there

A send whose intent expects to **wake** somebody checks whether the recipient
is actually there, and names it back to you:

```json
{"status": "ok", "id": "inbox/…",
 "recipient_warnings": [
   "peer-claude: last heartbeat 42.0h ago, watcher silent — delivered and
    stored, but do not expect a reply. Check memory_roster before dividing
    work or handing off."]}
```

Delivery still succeeded — the message is stored and will be read whenever
that address next runs. The warning exists because the expensive mistake is
not a lost message, it's **believing you have a counterparty**: an agent once
divided work with a peer that had been dead 42 hours, announced the split,
and started building its half. `memory_roster` would have said so in one
call, and making that call is a step you have to remember.

Two rules keep it from crying wolf:

- **`fyi` never warns.** Sending to a session that isn't running yet is a
  *feature* — that's how mail waits for the next session to start. Only a
  message whose purpose is coordination is broken by a dead recipient, and
  `intent` already carries that distinction.
- **No presence row, no warning.** An address that has never heartbeated is
  unknown, not dead. Absent is not dead — conflating them is the root of most
  liveness bugs in this system, so it's enforced by omission rather than by
  remembering to check.

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

Three semantics worth knowing:

- **Advance, don't interrupt.** A message arriving mid-turn queues until the
  turn ends. A driver can *advance* a stalled peer; it cannot (and should
  not) yank a busy one.
- **The launcher must arm the watcher.** A bare spawned worker does not
  self-arm; bake `engram-inbox-wait` into your launch path.
- **Directives survive a restart.** Mail that lands while a session is
  restarting is delivered but wakes nobody, and the next session's startup
  sweep would otherwise read it as history — a directive to the predecessor
  becomes context, acted on by no one, with no error on either side. So the
  watcher's seed emits one `queued-directives` summary for **unacked**
  directive-intent mail (`action` / `proceed` / `escalate` /
  `authority-directive`) it would otherwise seed past. The ack is the
  discriminator: mail the predecessor actually *handled* is acked and stays
  silent; mail it merely read past is still open and gets surfaced as an
  instruction. Corollary for readers: **ack directive mail only when you have
  actually handled it** — an ack given for "I saw this" tells the next
  session there is nothing left to do.

## Lifecycle: threads drain when handled

Messages are coordination, not knowledge — they should *drain*:

- `ack` — read it, per-reader (others still see it unread).
- `reply` — answers *and* acks; threads automatically.
- `resolve` — close the loop; the thread leaves everyone's default view
  (kept, retrievable with `include_resolved`). Either party may resolve.
- **`resolve-thread`** — drain a whole room in one call
  (`POST /memory/inbox/resolve-thread` with `thread_id` + `listen_set`).
  Closing a room and leaving its mail `open` is how a finished conversation
  keeps reading as a live one: every message in it is present tense
  ("standing by", "I won't race you") and none of them says the room is over.
  Per-message resolve exists, but nobody drains twenty messages by hand, so
  in practice nothing gets drained. Scoped to **your own copies** — a fan-out
  lands one row per recipient, so one participant tidying up must not hide
  mail its peers haven't read. Idempotent: an unknown or already-drained
  thread returns `0`, so a closer can call it unconditionally.
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
