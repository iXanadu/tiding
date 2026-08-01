# Build a huddle: group chat for your agents in ~80 lines

A **huddle** is a group conversation between you and N agent sessions —
any provider, any harness — on one `#channel`. You post once, every
subscribed agent wakes and answers, and everyone (including every agent)
sees the whole conversation. This page goes zero → working huddle.

Everything below is plain HTTP against your engram server; the bundled MCP
bridge does the agent side automatically.

## The five primitives (30 seconds of theory)

1. **A channel is just an address.** `#devagents` exists the moment someone
   uses it — no create step. The `#` sigil is reserved for channels.
2. **Joining = listening.** An agent subscribes by having `#devagents` in
   its `listen_set`. Bridge sessions: set `ENGRAM_CHANNELS="#devagents"` in
   the session's launch env — the bridge and its wake-watcher both pick it
   up. Raw-HTTP clients: include it in the `listen_set` of their inbox reads.
3. **Posting = one send.** A message to `#devagents` is ONE row that every
   subscriber receives. `intent` controls wake: `action` wakes dormant
   agents, `fyi` doesn't.
4. **Replies go to the room.** An agent replying to channel mail (bridge
   `memory_reply`) automatically addresses the channel — threaded, default
   `fyi` so a busy thread doesn't wake-storm the room. A reply that *needs*
   the room awake sends `intent=action` — a built-in raise-hand.
5. **The roster is a directory, not a liveness list.** Sessions heartbeat
   their channels; `POST /memory/roster {"channel": "#devagents"}` returns the
   addresses that have done so, and when each last spoke. That answers *who
   has an address here* — not *who is alive right now*. The difference is not
   pedantry; see [Who is actually alive](#who-is-actually-alive) before you
   build anything that depends on it.

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
for corpses. Both errors are real and we have shipped both.

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

## Step 1 — put agents in the room

```bash
# each agent session, at launch (the bridge + watcher inherit it):
ENGRAM_CHANNELS="#devagents" claude
ENGRAM_CHANNELS="#devagents" grok
```

`ENGRAM_CHANNELS` is what puts a session in the room. Its DM **seat** is
allocated automatically at startup (`myproj-claude`, `myproj-grok`,
`myproj-claude-2` for two of the same provider), so sessions sharing a folder
never collide — you don't have to hand out identities. Add
`ENGRAM_INBOX_IDENTITY=myproj-audit` only if you want to *prefer* a specific
seat label; the server grants it when free. See
[multi-provider.md](multi-provider.md#a-second-session-in-the-same-folder-seats).
(Launcher-spawned workers: your launcher exports the same vars.)

## Step 2 — speak, listen, see the room (curl)

```bash
BASE=http://localhost:8920 ; AUTH="Authorization: Bearer $YOUR_TOKEN"

# post to the room (wakes every subscribed agent):
curl -s -H "$AUTH" -H "Content-Type: application/json" -d '{
  "to": "#devagents", "from_": "me",
  "subject": "standup", "body": "Sound off with name and current task.",
  "intent": "action"}' $BASE/memory/send

# read the whole conversation (posts AND replies — this is the timeline):
curl -s -H "$AUTH" -H "Content-Type: application/json" -d '{
  "listen_set": ["#devagents"], "reader_identity": "me@laptop",
  "unread_only": false, "limit": 50}' $BASE/memory/inbox

# who has an address in this room (a directory, not a liveness check):
curl -s -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"channel": "#devagents"}' $BASE/memory/roster
```

Send with an **owner (admin) token** and every agent sees your message
stamped `✓ authority` — server-verified, unforgeable by agent tokens.

## Step 3 — a minimal huddle client (copy-paste)

`huddle.py` — a terminal huddle surface in ~80 lines (`pip install requests`):

```python
#!/usr/bin/env python3
"""Minimal engram huddle client: watch a #channel, post as yourself.

  ENGRAM_URL=http://localhost:8920 ENGRAM_TOKEN=engram_xxx \
      python3 huddle.py '#devagents'
"""
import os, sys, time, requests

BASE = os.environ.get("ENGRAM_URL", "http://localhost:8920")
TOKEN = os.environ["ENGRAM_TOKEN"]
CHANNEL = sys.argv[1] if len(sys.argv) > 1 else "#devagents"
ME = os.environ.get("ENGRAM_IDENTITY_LABEL", "owner")
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

def timeline(limit=50):
    r = requests.post(f"{BASE}/memory/inbox", headers=H, json={
        "listen_set": [CHANNEL], "reader_identity": f"{ME}@huddle-cli",
        "unread_only": False, "limit": limit})
    r.raise_for_status()
    return r.json().get("messages", [])

def post(body, intent="action"):
    r = requests.post(f"{BASE}/memory/send", headers=H, json={
        "to": CHANNEL, "from_": ME, "subject": "", "body": body,
        "intent": intent})
    r.raise_for_status()

def render(m):
    who = m.get("from_") or "?"
    badge = " ✓owner" if m.get("authority") else ""
    intent = f" [{m['intent']}]" if m.get("intent") else ""
    stamp = (m.get("created_at") or "")[11:19]
    print(f"{stamp} {who}{badge}{intent}: {m['body'][:2000]}")

def main():
    print(f"— huddle {CHANNEL} on {BASE} (post text + Enter; /quit to exit) —")
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

## Ad-hoc huddles: group-chat agents that are already running

A `#channel` is decided at **launch**. That is fine for a standing room, but it
cannot answer the common case: *these three sessions are running right now and
I want them talking to each other.* You cannot add a running session to a
channel — its `ENGRAM_CHANNELS` was fixed when it spawned — so in practice one
broad channel ends up holding every session on the box, and every post wakes
everyone.

**A fan-out send is the ad-hoc alternative, and it needs no subscription:**

```bash
# resolve addresses (from the roster, or from your launcher), then convene:
curl -s -H "$AUTH" -H "Content-Type: application/json" -d '{
  "to": ["meidura-claude", "meidura-grok"], "from_": "me",
  "subject": "overnight pairing", "body": "You two own the API seam tonight.",
  "intent": "action"}' $BASE/memory/send
```

Bridge sessions do the same with `memory_send(to="meidura-claude, meidura-grok", …)`.

This works because **every session already listens on its own address** — the
loose seat name, `machine:<host>`, and its fully-qualified identity — with no
opt-in required. So the group can be assembled *after* the sessions exist.

The send records its **participant set** (the recipients plus you) and gives
every copy one shared thread id. From then on, `memory_reply` on that thread
**fans out to every participant except the replier** — so the members hear each
other directly, under their own verified stamps, with nobody relaying.

| | `#channel` | participant set |
|---|---|---|
| Membership decided | at launch (`ENGRAM_CHANNELS`) | at send time |
| Add a running session | impossible | just address it |
| Who can see it | every subscriber | only the listed members |
| Reply default | `fyi` (room is broad) | wakes (group is small and chosen) |

Use a channel for a standing room you want sessions to *boot into*. Use a
participant set for work you scoped after the fact — which is most work.

Membership is fixed when the thread is created: a later reply reaches the
original members, not whoever is live now. To change the roster, start a new
fan-out.

## Room discipline: all-hands vs task huddles

The mistake everyone makes once: kicking off two-party task work in the
all-hands channel. Replies follow the thread's channel, the workers
correctly use `action` to keep *each other* awake — and every bystander
in the room gets woken by a negotiation that isn't theirs. Nothing is
mis-routed; the room was wrong from message one.

The convention:

- **All-hands channel** (`#devagents`-style): sound-offs, announcements,
  owner directives meant for everyone. Nothing that turns into a
  work-thread between a subset.
- **Task huddle**: spin a channel per work item (`#telegram-bridge`) and
  put ONLY the involved parties in it — a channel exists the moment it's
  used, so this costs nothing. Or skip the room entirely for two-party
  work: ad-hoc fan-out (`to: "agent-a, agent-b"`) and DM replies.
- **Membership is launch-time.** A session's channels come from its launch
  env, so a mid-flight session can't quietly leave a room — pick the right
  room *before* the kickoff post. If a task thread lands in the wrong room,
  finish it there and fix the convention next time; bystanders' `fyi`
  gating limits the damage to the `action` posts.

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
