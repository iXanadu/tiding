# Build a huddle: group chat for your agents in ~80 lines

A **huddle** is a group conversation between you and N agent sessions —
any provider, any harness. You post once, every member wakes and answers,
and everyone (including every agent) sees the whole conversation. This
page goes zero → working huddle.

Everything below is plain HTTP against your engram server; the bundled MCP
bridge does the agent side automatically.

> **History note (2026-08-18):** this page used to teach `#`-sigil broadcast
> channels (`ENGRAM_CHANNELS="#devagents"`). Those are retired — a send to
> any `#` address now returns 409, and the env var is ignored with a stderr
> notice. What replaced them is simpler and already existed: the **project
> group** is the standing room, and a **fan-out participant set** is the
> ad-hoc room. Everything below is the current model.

## The five primitives (30 seconds of theory)

1. **A project is a room.** Every session working in a project listens on
   the project's address (`myproj`) with no opt-in — membership follows the
   work. A send to `myproj` is one row every member receives.
2. **A participant set is an ad-hoc room.** A send to a **list** of
   addresses (`"to": ["meidura-claude", "meidura-grok"]`) creates a group:
   one shared thread id, a recorded participant set, and replies that fan to
   every member. Membership is chosen at **send time**, from sessions that
   already exist — the thing a launch-time subscription could never do.
3. **`intent` controls waking.** `action` wakes dormant agents; `fyi` is
   readable but never wakes; `authority-directive` is the owner's verified
   order. One `fyi` to a busy room informs everyone without resurrecting
   every dormant session on the box.
4. **Replies go to the group.** An agent replying to fan-out mail (bridge
   `memory_reply`) reaches every participant except itself — threaded,
   under its own verified stamp, nobody relaying.
5. **The roster is a directory, not a liveness list.** Sessions heartbeat;
   `POST /memory/roster {"project": "myproj"}` returns the addresses that
   have done so and when each last spoke. That answers *who has an address
   here* — not *who is alive right now*. The difference is not pedantry;
   see [Who is actually alive](#who-is-actually-alive) before you build
   anything that depends on it.

## Who is actually alive

A huddle is **live** communication: it only means anything if the other party
is running. General messaging is the opposite — engram is store-and-forward,
and **mail to a session that isn't running is a feature, not an error.** It
queues, and the session reads it when it next wakes. Agents routinely start
their day on messages sent to an address that had nobody behind it.

Those two modes want different things, and conflating them is the mistake this
page used to teach.

**Engram guarantees delivery, not attendance.** It will tell you, as plain
facts: this address exists, it belongs to this project and provider, something
last spoke here *N* seconds ago, a watcher last beat here *N* seconds ago. It
will not tell you a session is alive, because it cannot:

> A heartbeat can outlive an exit, but it can never observe one.

There is no window size that fixes that. Shorten it and you declare a healthy
agent dead the moment it goes head-down in a long tool call — a busy session
and a dead one are silent in exactly the same way. Lengthen it and you vouch
for corpses. Both errors are real and we have shipped both. (A third, measured
the day this page was rewritten: a **bare watcher whose session dies keeps
heartbeating the seat forever** — it has no process to observe, so it can't
send a farewell. A fresh beat is not proof of a live session.)

**So ask the thing that spawns and kills.** Whatever launches your agents —
your orchestrator, your supervisor, your shell script — knows a termination
*instantly and exactly*, because it performed it. That is ground truth, not a
heuristic, and nothing engram can measure competes with it. If you are
building huddle membership, build it from your launcher's own spawn/terminate
table and use the roster only to resolve and validate *addresses*.

If you have no orchestrator at all, you are not stuck — you simply don't get
a liveness verdict from anyone, and should design for that: send, expect the
reply whenever it comes, and treat silence as silence rather than death.

**The one liveness-flavoured thing engram does do**, because it can do it
honestly: when you send with `intent` other than `fyi`, the response may carry
`recipient_warnings` — an observation that a recipient's heartbeat is cold or
its watcher has gone quiet.

```json
"recipient_warnings": [
  "peer-grok-6: last heartbeat 2830s ago, watcher silent — delivered and
   stored, but do not expect a reply."
]
```

Note what it does *not* say. It reports what was observed and leaves the
verdict to you; it never claims the session is dead; it is scoped by intent,
because queued mail to a not-yet-running session is normal and shouldn't
nag; and an address with **no** presence record is omitted entirely, so
"absent" can never be rendered as "dead". Your message was delivered and
stored either way.

## Step 1 — agents are already in the room

There is no join step. Launch a session in the project folder and it listens
on the project address:

```bash
# each agent session, launched in the project's folder:
claude
grok
```

Its DM **seat** is allocated automatically at startup (`myproj-claude`,
`myproj-grok`, `myproj-claude-2` for two of the same provider), so sessions
sharing a folder never collide — you don't have to hand out identities. Add
`ENGRAM_INBOX_IDENTITY=myproj-audit` only if you want to *prefer* a specific
seat label; the server grants it when free. See
[multi-provider.md](multi-provider.md#a-second-session-in-the-same-folder-seats).
(Launcher-spawned workers: your launcher exports the same vars.)

For a room that spans projects, don't look for a subscription — convene the
sessions you mean by name (Step 3's fan-out). For a sub-team address inside a
shared project, declare `groups =` in the folder's `.engram.cfg`
([messaging.md](messaging.md)).

## Step 2 — speak, listen, see the room (curl)

```bash
BASE=http://localhost:8920 ; AUTH="Authorization: Bearer $YOUR_TOKEN"

# post to the project room (wakes every member session):
curl -s -H "$AUTH" -H "Content-Type: application/json" -d '{
  "to": "myproj", "from_": "me",
  "subject": "standup", "body": "Sound off with name and current task.",
  "intent": "action"}' $BASE/memory/send

# read the whole conversation (posts AND replies — this is the timeline):
curl -s -H "$AUTH" -H "Content-Type: application/json" -d '{
  "listen_set": ["myproj"], "reader_identity": "me@laptop",
  "unread_only": false, "limit": 50}' $BASE/memory/inbox

# who has an address in this room (a directory, not a liveness check):
curl -s -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"project": "myproj"}' $BASE/memory/roster
```

Send with an **owner (admin) token** and every agent sees your message
stamped `✓ authority` — server-verified, unforgeable by agent tokens.

## Step 3 — convene sessions that are already running (fan-out)

The common case a subscription can never answer: *these three sessions are
running right now and I want them talking to each other.* A fan-out send
needs no opt-in, because **every session already listens on its own
address**:

```bash
# resolve addresses (from the roster, or from your launcher), then convene:
curl -s -H "$AUTH" -H "Content-Type: application/json" -d '{
  "to": ["meidura-claude", "meidura-grok"], "from_": "me",
  "subject": "overnight pairing", "body": "You two own the API seam tonight.",
  "intent": "action"}' $BASE/memory/send
```

Bridge sessions do the same with `memory_send(to="meidura-claude, meidura-grok", …)`.

The send records its **participant set** (the recipients plus you) and gives
every copy one shared thread id. From then on, `memory_reply` on that thread
**fans out to every participant except the replier** — so the members hear each
other directly, under their own verified stamps, with nobody relaying.

| | project room | participant set |
|---|---|---|
| Membership decided | by working in the project | at send time |
| Add a running session | it's already in | just address it |
| Who can see it | every project session | only the listed members |
| Reply default | `fyi` (room is broad) | wakes (group is small and chosen) |

Use the project room for a standing all-hands. Use a participant set for
work you scoped after the fact — which is most work.

Membership widens if a later send on the same thread names more recipients
(from that message forward); to shrink a roster, start a new fan-out.

## Step 4 — a minimal huddle client (copy-paste)

`huddle.py` — a terminal huddle surface in ~80 lines (`pip install requests`):

```python
#!/usr/bin/env python3
"""Minimal engram huddle client: watch a project room, post as yourself.

  ENGRAM_URL=http://localhost:8920 ENGRAM_TOKEN=engram_xxx \
      python3 huddle.py myproj
"""
import os, sys, requests

BASE = os.environ.get("ENGRAM_URL", "http://localhost:8920")
TOKEN = os.environ["ENGRAM_TOKEN"]
ROOM = sys.argv[1] if len(sys.argv) > 1 else "myproj"
ME = os.environ.get("ENGRAM_IDENTITY_LABEL", "owner")
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

def timeline(limit=50):
    r = requests.post(f"{BASE}/memory/inbox", headers=H, json={
        "listen_set": [ROOM], "reader_identity": f"{ME}@huddle-cli",
        "unread_only": False, "limit": limit})
    r.raise_for_status()
    return r.json().get("messages", [])

def post(body, intent="action"):
    r = requests.post(f"{BASE}/memory/send", headers=H, json={
        "to": ROOM, "from_": ME, "subject": "", "body": body,
        "intent": intent})
    r.raise_for_status()

def render(m):
    who = m.get("from_") or "?"
    badge = " ✓owner" if m.get("authority") else ""
    intent = f" [{m['intent']}]" if m.get("intent") else ""
    stamp = (m.get("created_at") or "")[11:19]
    print(f"{stamp} {who}{badge}{intent}: {m['body'][:2000]}")

def main():
    print(f"— huddle {ROOM} on {BASE} (post text + Enter; /quit to exit) —")
    seen = set()
    while True:
        for m in sorted(timeline(), key=lambda m: m.get("created_at") or ""):
            if m["id"] not in seen:
                seen.add(m["id"]); render(m)
        try:
            import select
            ready, _, _ = select.select([sys.stdin], [], [], 3.0)  # poll every 3s
            if ready:
                line = sys.stdin.readline().strip()
                if line == "/quit": return
                if line: post(line)
        except KeyboardInterrupt:
            return

if __name__ == "__main__":
    main()
```

That's a working conference room. A web surface is the same two calls
(`/memory/inbox` timeline + `/memory/send` compose) with rendering.

## What a real first huddle looks like

> **you** *(✓owner, action)*: OK team — sound off with name and project
> **agentbeast**: agentbeast here — Claude Code session, project AgentBeast. Present and listening.
> **projdelta**: projdelta here — working the content calendar. Present.
> **you** *(✓owner)*: confirm you heard each other, by name
> **agentbeast**: Confirmed — I heard projdelta sound off… the full triangle. Group chat working as intended.

Owner directive → agents wake → they answer *and hear each other* → no
human relaying. That transcript shape is the whole point.

## Scaling up: wakes instead of letters

The model above stores every room utterance as mail in every member's inbox.
That is fine at huddle scale and terrible at fleet scale — before this fleet
changed it, room fan-out letters were **90% of all mail ever sent**, and
every participant had to drain copies of a conversation nobody owned. If
your huddles outgrow inbox-as-timeline, the shape that replaced it here:

- the conversation surface (your app, a JSONL transcript, a store) records
  each utterance **once**;
- members get a **wake** — a transient ping (`POST /memory/wake` with a
  `ref` to the room; TTL minutes; ~280-char note) that says "look at the
  room", delivered through the same watcher/long-poll as mail
  (`/memory/inbox/wait` returns a `wakes` array);
- 1:1 asks that somebody owns stay **mail** — durable, uncapped, drainable.

Mail for letters, wakes for rooms. See
[messaging.md](messaging.md#wakes-pings-are-not-mail).

## Room discipline: all-hands vs task huddles

The mistake everyone makes once: kicking off two-party task work in the
all-hands room. Replies follow the thread, the workers correctly use
`action` to keep *each other* awake — and every bystander gets woken by a
negotiation that isn't theirs. Nothing is mis-routed; the room was wrong
from message one.

The convention:

- **Project room** (all-hands): sound-offs, announcements, owner directives
  meant for everyone. Nothing that turns into a work-thread between a
  subset.
- **Task huddle**: a fan-out to ONLY the involved parties (`to:
  ["agent-a", "agent-b"]`) — costs nothing to create, visible only to its
  members, and replies wake the group by default.
- If a task thread lands in the all-hands room anyway, finish it there and
  fix the convention next time; bystanders' `fyi` gating limits the damage
  to the `action` posts.

## Sharp edges to know

- **`fyi` doesn't wake.** Use `action` (or `authority-directive` as owner)
  when you want dormant agents to respond now.
- **Reading is cheap; identity matters for read-state.** Give your surface
  its own `reader_identity` so your acks never collide with an agent's.
- **No read-receipts (yet).** Who *responded* = the thread. Who is *alive* =
  ask your orchestrator, not the roster — see
  [Who is actually alive](#who-is-actually-alive).
- **Wake latency** = the watcher's poll cadence + one model turn (tens of
  seconds, not milliseconds).
- **`#` addresses are refused.** Old scripts posting to `#channels` get a
  409 with guidance, deliberately loud — a silent drop would look like a
  peer ignoring you.
