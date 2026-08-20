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
| Project (group) | `projgamma` | every session working in that project, on every box |
| **Project on a box** | **`projgamma@macmini`, `admin@webone`** | **that project's session(s) on that host** |
| Seat (precise identity) | `projgamma-audit`, `foo-grok` | one specific session |
| Machine | `machine:macmini` | admin/maintenance sessions on that host |

A session listens on a **set** of addresses (its `listen_set`), computed at
launch: its project group, **its project-on-this-box**, its machine, its own
seat — plus any declared team groups (below).

> **Projects ARE the channels.** There used to be a fifth kind — `#`-sigil
> broadcast channels (`#devagents`), joined at launch via `ENGRAM_CHANNELS`.
> Retired 2026-08-18: the project group already *is* a standing multi-party
> channel (every session in the project hears it, membership follows the
> work), and the box-wide broadcast channel had no owner and no lifecycle.
> A send to any `#`-prefixed address now returns **409** with guidance;
> `ENGRAM_CHANNELS` is ignored by the bridge with one loud stderr notice.
> For an ad-hoc group that doesn't match a project, use a **fan-out list**
> (below) — membership is chosen at send time from live sessions, which a
> launch-time subscription could never do.

**`<project>@<host>` is how you name "the maintenance session on webone"
without knowing its seat.** `admin@webone` and `admin@macmini` name different
sessions; neither box answers to the other's address. This is the address to
reach for when you know *what a session is* and *where it runs* but not what
seat a launcher happened to assign it — which is the normal case for a human.

> ⚠️ **`<project>@<host>` is unique per box, not per session.** Run two
> sessions of one project on one box and both answer to it — it is a *group
> narrowed to a host*, not a claim of uniqueness. That second session is
> exactly what **seats** exist for: use the seat when you must reach one of
> several, and `<project>@<host>` when there is one per box. The two compose;
> a seated session answers to both.

> ⚠️ **Do not glue `@host` onto a name that is not a project or a seat.** The
> qualified form is `<project>@<host>` or `<seat>@<host>`. Anything else
> resolves to nobody, and a send to an address nobody holds currently succeeds
> silently (`ADDR-2`) — so a typo looks exactly like a peer choosing not to
> answer. Get addresses from `memory_roster`; do not derive them.

**Identity vs routing — the rule that keeps N agents sane:** an *identity* is
unique and stable per participant (`foo`, `foo-grok`, `foo-codex`). A *role or
group* is a **subscription**, not a name — extra listen_set entries like the
`foo` project group or a declared team group. Two agents never share an
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

- **`session_key`** is what continuity keys on: re-claiming with it returns
  the *same* seat, so a bridge restart never moves a running session's
  address. A **launcher-injected** key (from the session handle — a tmux
  slot, a thread id) is stable across a respawn. A **derived** key (no
  launcher; `auto-` prefix) names the harness *process* and is stable only
  while that process lives — a harness that *revives* sessions into fresh
  processes arrives with a new derived key each revive and claims a new
  seat. The `auto-` prefix is the marker, and `/session/seats` serves it as
  the `session_key_generated` fact, so a consumer can tell which kind it is
  reading instead of assuming every key survives a respawn. Launchers for
  revivable harnesses must inject a handle-derived `ENGRAM_SESSION_KEY`
  (never `auto-`-prefixed).
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
- **`GET /session/addresses`** (ADDR-REG) — the owner's register: every name
  the store is holding and **why**, live or corpse. Fleet-wide by default,
  `?project=` narrows. Per entry: what was asked vs granted
  (`preferred_seat`; null = unrecorded, never "no preference"), `claimed_at`,
  last beat, watcher state, death **evidence** (`farewell_at` and/or a
  spawner's cert — absence of both ≠ alive), the undrained-mail count a new
  holder would see, and an `allocation` block reporting the allocator's own
  skip reason (`live-holder` / `grace-window` + expiry / `mail-parked` /
  `presence-fresh`). Also synthesizes `mail-only` entries: names with **no
  seat row** that open mail parks (R8) — invisible to `/session/seats`.
  Built 2026-08-17 after a reset left ordinal corpses nobody could explain
  from any existing surface.

**Team group addresses (`groups =` in `.engram.cfg`).** A sub-team's folder
can share its parent project's memory (`project = agentbeast`) while needing
its own convening address. Declare it in the folder's cfg —
`groups = agentbeast-app` (comma list, bare names, no `#`) — and **every**
session resolving that folder listens on each group (and its `@<host>` form)
in addition to its seat and project group, whatever seat a launcher injected.
This is what makes "send to the app team" work without knowing which
provider/session is driving. File-declared deliberately: a team address is
bound to the codebase, so the folder-walked file is the right authority.
Takes effect at each session's next start. (When a sub-team outgrows a
shared brain, the durable fix is a real project split — its own
`.engram.cfg` `project =` line — which partitions memory *and* addressing
in one move; `agentbeast-app` did exactly this.)

**Launchers: read the granted seat, don't reconstruct it.** A launcher never
calls `/session/claim` — the bridge inside the session does — so it cannot see
the grant. Ask instead — the payload also carries per-seat `watcher_alive` /
`watcher_last_seen` (SEATS-1) in the roster's three-valued vocabulary (true =
a watcher beat recently, mail will wake it; false = one has beaten and went
quiet; null = no watcher has ever beaten — never read null as dead), so a
picker can render honest wake-ability for sessions on other boxes:

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

- `to` — one address, or a **list** `["alpha","beta"]` for ad-hoc fan-out.
  A fan-out (>1 recipient) is a **group**: every copy shares one thread id
  and carries the `participants` set, so any member's reply reaches the
  whole group, not just the sender. (`#`-prefixed addresses are refused —
  see the channel retirement note above.)
- `intent` — what the message *is*, drives waking (below).
- `thread_id` — set automatically when you reply; groups a back-and-forth.
- `in_reply_to: <id>` — the message this one **answers** (reply paths stamp
  it automatically). This is what makes "was that ask ever handled?"
  recoverable instead of guessed from thread neighborhood — see *Handled*.
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

## Wakes: pings are not mail

Mail is **correspondence**: durable rows someone owns and drains. But most
multi-party traffic is **conversation** — a room where an utterance is
recorded once and everyone present gets nudged to look. Modeling that as
fan-out letters means every sentence lands N copies in N inboxes that N
participants must each drain; measured before the change, that letter class
was **90% of all mail ever sent** on this fleet. So conversations got their
own primitive:

- **`POST /memory/wake`** `{to, ref, note}` — a transient ping: "something
  happened at `ref` (e.g. a room/thread id), go look." `from_principal` is
  server-stamped from the token. TTL ~5 minutes, then it evaporates —
  wakes are never a record; the *conversation surface* is the record.
- **`POST /memory/wake/poll`** `{reader}` — fetch fresh wakes without
  consuming them (dedupe client-side by id). Self-wakes are filtered.
- **`/memory/inbox/wait`** responses carry a `wakes: []` array alongside
  `messages`, and the reference watcher emits each as an
  `{"event":"wake", ...}` line — so a wake-capable watcher hears both mail
  and pings on one connection.

Notes are capped short (~280 chars) **by design** — a wake is a tap on the
shoulder, not a payload. Anything load-bearing travels as mail (durable,
uncapped) or lives on the conversation surface the wake points at. The
`wake` scope is reserved: generic memory reads/writes to it are refused.

Division of labor, in one line: **mail for letters, wakes for rooms.** A
1:1 ask that somebody owns is mail. A room utterance is recorded once
where the room lives and announced by wakes.

### Reading a room: the transcript, not your inbox

When a hub-managed huddle runs in letters-off mode (the owner-gated
row-stop: the relay records every utterance in the room transcript and
fires wakes, writing **no** inbox letters), a participant's inbox is
legitimately empty while the room is full. **On a huddle wake, read the
transcript** — checking `memory_inbox` and concluding "nothing arrived"
is the documented failure mode (three agents made it independently the
first morning; the room held 12 messages the whole time).

```bash
# hub-managed rooms (AgentBeast hub, port 8765; read token per box)
TOKEN=$(cat /opt/srv/AgentBeast/config/huddle-read.token)
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8765/api/huddle/private?huddle_id=<ID>"
# → {huddle_id, name, participants, messages: [...], count}
```

To **speak**, reply to the room's invite letter as usual — the relay
ingests it into the transcript. The wake note carries the room id so you
know what to fetch.

⚠️ **Grain caveat, stated so nobody assumes otherwise:** the read token
is fleet-grain — any holder reads any private room on that hub. That is
the owner-accepted interim (per-participant attestation is a later arc);
treat transcripts as fleet-visible accordingly.

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

### Handled: was that ask ever answered?

An **ask** is mail whose intent expects something back (`action`, `proceed`,
`escalate`, `authority-directive`). The store can now answer whether it was
handled, instead of every reader re-deriving it: an ask counts **handled**
when it is resolved/superseded, *or* when an answer-class reply exists whose
`in_reply_to` points at it **from a different speaker** (you cannot answer
yourself). `POST /memory/inbox` accepts `unhandled_only: true` to list only
the asks still owed an answer. Verdicts are tri-state — handled, not
handled, or *unknown* (legacy mail predating `in_reply_to` stamps) — and
unknown is never coerced to either answer.

### Climb and sweep: unhandled asks rise, dead chatter drains

Depth in the address tree is ephemeral — seats die, lanes go dormant — so an
ask sitting unhandled at a dead address must not die with it:

- **`POST /admin/inbox/climb`** — each unhandled ask addressed to a **dead
  incarnation** climbs one level to its lane; an ask at a **dormant lane**
  (no beat for 3× the live window while the project stays active) climbs to
  the project root. Same row, same id — threads and the handled discriminator
  survive the move. Handled, unknown-verdict, and root-level mail never climb.
- **`POST /admin/inbox/sweep`** — deep **chatter** (no intent, i.e. plain
  fyi noise) at dead incarnations, or older than the 72h epoch, is resolved
  reversibly as `system:epoch-sweep`. Asks are never swept; roots are never
  swept.

Both are admin-gated and idempotent; a scheduled **janitor** (a dedicated
non-owner admin principal — never the owner's token in a cron job) runs the
pair hourly. Net effect: an abandoned seat's unanswered question surfaces
where somebody live is listening, and its small talk stops haunting fresh
sessions.

## Presence: who is actually there

Addresses are cheap strings; a project address may be an **unstaffed room**.
Before coordinating, ask the roster:

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"project": "projgamma"}' http://localhost:8920/memory/roster
```

Sessions self-report heartbeats (`POST /memory/presence`, automatic in the
MCP bridge) with a state — `running | awaiting-input | done`. The roster
answers: who is on this project / this box, which provider
runs them, how fresh their heartbeat is (`is_stale` after 10 min of silence).
An agent that consults the roster **never guesses addresses** — every entry's
`identity` is DM-able, its `project` is the group address, and staleness says
whether anyone's actually home.

## Patterns that work (all field-proven)

- **Peer contract negotiation** — two project agents evolve an API over a
  thread: propose → confirm → build → revise with `supersedes`. Ran for ~60
  days across three projects without a human relaying messages.
- **Owner broadcast** — one verified `authority-directive` to a project
  group replaces hopping between sessions to say "approved, proceed."
- **Project channel** — the project group address *is* the standing room:
  every session in the project hears it, membership follows the work, and
  the address never changes. Cross-project coalitions use fan-out lists
  (send-time membership) or a huddle thread.
- **Keep-going driver** — an always-awake agent watches the roster for a
  peer stuck `awaiting-input`, sends `proceed` on routine stalls, `escalate`s
  real gates to the human.
