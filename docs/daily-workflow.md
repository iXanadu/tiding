# The daily workflow: living with a memory-first agent

How a human and their coding agents actually use engram day to day. Everything
here is the workflow engram itself is built with — dogfooded every session,
not aspirational. It assumes the [getting-started](getting-started.md) setup
is done and your agent has the memory tools (MCP bridge or HTTP).

The core idea: **the agent's memory, not chat scrollback, is the project's
state.** A session that dies loses nothing that mattered, because everything
that mattered was stored deliberately.

## The session loop

### 1. Startup: recall before work

Every session starts by *reading memory before touching code*:

- **Get the handoff** — `memory_get key=startup/next scope=project`. The
  previous session wrote it as its last act: where things stand, what's next,
  what will bite you.
- **Check for interrupted work** — `memory_search query="wip" scope=project`.
  A `wip/current` entry means the last session died mid-task; pick it up.
- **Sweep for context** — a few searches over `scope=project` ("session",
  "decision", "strategy") plus `scope=shared` for cross-project lessons.
  The handoff is one moment in time; the sweep catches what it missed.
- **Read the inbox** — `memory_inbox`. Other agents may have left messages
  while this session didn't exist. Durable delivery means nothing was lost.

Wire this into a startup command/skill so it happens every time, not when
someone remembers.

### 2. During work: store at milestones

Memory writes are deliberate, not a firehose. The rhythm:

| Moment | Write |
|---|---|
| A decision gets made | `decision/<what>` — the call, the why, the alternatives rejected |
| Something non-obvious gets fixed | `fix/<what>` or `lesson/<topic>` — future sessions search for exactly this |
| A milestone lands | update `wip/current` — one entry, overwritten, always current |
| You learn something any project could use | store it at `scope=shared`, not buried in one project |

Key naming is a convention worth enforcing: `session/YYYY-MM-DD-desc`,
`decision/what`, `fix/what`, `lesson/topic`. Searchable prefixes beat clever
prose.

**What not to store:** secrets/tokens (ever), large code blocks (reference a
file path — git already stores the code), transient state that's stale by
tomorrow.

### 3. Wrapup: hand off to a session that doesn't exist yet

The last act of a session is writing for the next one:

- **Session story** — `session/YYYY-MM-DD-<desc>`: what happened, what
  changed, what's verified vs. assumed.
- **The handoff** — overwrite `startup/next`: current state, first job for
  next time, the gotchas that will bite. Write it for a reader with zero
  context — that reader is real.

A good handoff names commits, not vibes: "dev @ abc1234, prod restarted and
health-verified" beats "everything's deployed I think."

## The team loop (multi-agent)

When more than one agent works your projects, the same store grows a
messaging layer — see [messaging.md](messaging.md) for mechanics,
[build-a-huddle.md](build-a-huddle.md) for group chat.

Daily patterns that emerge:

- **Ask, don't interrupt.** An agent that needs another project's input sends
  to that project's address and *keeps working*. The answer arrives whenever
  the other side is next alive — or instantly, if it runs the watcher.
- **The wake stream makes agents reachable.** The bridge spawns a watcher
  for every session; attaching its stream at session start (the command
  `memory_status` prints, under Monitor) means an agent wakes on inbound mail
  instead of discovering it tomorrow. Without it, mail still delivers — it
  just waits.
- **Intent gates the wake.** Send `intent=action` when you need the recipient
  awake; `fyi` for everything else. A busy channel full of `fyi` wakes nobody.
- **Close your threads.** `memory_resolve` when a thread is done. Open mail
  is a to-do list; resolved mail is history. (Stale read mail auto-resolves
  after 72h so the pile can't grow forever.)
- **The human is a sender too.** Post to a project's address and every agent
  session on that project sees it — the "sound off" pattern: one broadcast,
  each agent replies, cross-hearing confirmed. For a group that spans
  projects, name the sessions in a fan-out list instead.

## The ledger habit (deferred work)

Memory decays in relevance; git does not. Deferred work lives in a
git-tracked `BACKLOG.md` at the repo root — terse, ID'd lines (`SEC-2`,
`DOC-7`) that commits can reference. Memory holds the *stories* (why a thing
was decided, how a fix worked); the ledger holds the *queue*. Every session
reads it at startup; done = delete the line in the same commit as the fix.

## Why this beats scrollback

- **Continuity is free.** Sessions crash, computers restart, context windows
  fill. The next session recovers in one `memory_get`.
- **Knowledge compounds.** A lesson stored at `scope=shared` in project A
  surfaces in project B's search six weeks later. Chat history can't do that.
- **Coordination is asynchronous by default.** No agent blocks on another
  being alive. The inbox is durable; presence is optional acceleration.
- **The human stays in command.** You read the same memory, send to the same
  addresses, and see the same roster the agents do — one shared reality, not
  N private transcripts.
