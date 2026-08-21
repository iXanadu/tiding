---
name: startup
description: When a new session begins in this project, run this to orient on project state, memory, and recent work before doing anything else.
---

Session startup. Follow these steps:

## 0. Orientation Prelude

Do both of these before any `scope=project` memory calls or substantive work.

**0a. Do NOT re-read what is already in context.** `~/.claude/CLAUDE.md` (→ `~/.agents/AGENTS.md`), the project `CLAUDE.md`, and `MEMORY.md` are injected into your system prompt at launch — re-`cat`ting them costs ~1k tokens each and adds nothing. Skim them *in the prompt* if you need to refresh a rule.

**Tool schemas — one `ToolSearch` call, only what startup needs:** `memory_get, memory_search, memory_inbox, memory_status, memory_store, memory_reply, memory_ack, Monitor`. Load `memory_send`, `memory_roster`, `memory_take_seat`, `memory_resolve_thread`, `memory_keys` later, only if a step below actually needs them.

**0b. Confirm project identity.** Check `.engram.cfg` at the repo root.

- If present: parse `project = <name>`. This is the canonical user_id for all `scope=project` memory and inbox addressing. Proceed.
- If absent:
  1. **Only** auto-suggest a name when CWD matches `~/projects/<name>/` (one level directly under `projects/`, at the repo root) AND `<name>` is NOT a generic deploy label (`prod`, `dev`, `staging`, `main`, `trunk`, `current`, `release`, `live`). In that case, suggest `<name>` and ask the user to confirm.
  2. Otherwise (nested layouts like `~/projects/site/sub/`, domain-style `~/projects/site.com/dev/`, server paths like `/var/www/site/prod`, or anything that doesn't match the clean `~/projects/<name>/` shape), do NOT guess — ask the user directly for the canonical project name. Do not infer from git remotes or path segments in ambiguous cases; the user decides.
  3. Once the user confirms the name: write `.engram.cfg` at the repo root containing `project = <name>`. Stage it. Commit (`Add .engram.cfg — canonical project identifier`).

## 1. Read Handoff Note

`memory_get` key=`startup/next` scope=project — this is the handoff from the last session.
If it references other memory keys, fetch those too.

Note the date the handoff was written. If it's more than a day or two old, treat it as a starting point rather than a complete picture — the exhaustive search in Step 3 may surface newer state the handoff doesn't mention.

## 2. Check for Interrupted Work

`memory_search` query="wip" scope=project limit=3 — if `wip/current` exists, the last session was interrupted. Read it, orient, and plan to continue.

## 3. Memory Recovery — sized to the handoff's age

`memory_search` returns full bodies; a 10-result query costs 2–4k tokens. Spend it where the handoff is weak, not by reflex.

**If `startup/next` is < 48h old (the normal case):** run ONLY
1. `memory_search` query="session" scope=project limit=3 — confirms nothing newer than the handoff exists.
Everything else (decisions, strategy, shared lessons, machine facts) is stable knowledge already banked in the handoff and `MEMORY.md`; fetch a specific key with `memory_get` only when a step needs it.

**If the handoff is older than 48h, or missing, or `wip/current` exists:** full sweep, in parallel —
1. `memory_search` query="session" scope=project limit=5 — recent session summaries
2. `memory_search` query="decision architecture" scope=project limit=5 — design decisions
3. `memory_search` query="strategy direction goals" scope=project limit=3 — strategic context
4. `memory_search` query="engram memory semantic" scope=shared limit=3 — cross-project lessons relevant to engram
5. `memory_search` scope=machine limit=3 — local env, paths, services

### Reconcile
Compare dates. If the most recent memories are newer than `startup/next`, the handoff is incomplete — note the gap and lead with the newer state.

## 4. Check Inbox

`memory_inbox` — messages from other Claude instances. Read and reply to anything relevant or actionable.

## 4a-bis. Are you co-working? Take a seat BEFORE attaching the wake stream

If the user says another agent is (or will be) working in this same folder —
"you're co-working", "grok is on this too", "you two are pairing" — this
session needs its own inbox seat. Without one, both sessions resolve to the
same identity: they share read-state, and each one's mail looks like its own
echo, so **they cannot wake each other at all**.

1. `memory_roster` (project filter) — see who already holds a seat here.
2. `memory_take_seat(name="<project>-<role>", project_dir=<repo-abs-path>)`.
   Discriminate by **role** (`-audit`, `-build`, `-remediate`); use the
   provider (`-grok`, `-claude`) only when that is the real distinction.
   Don't take a seat a peer already holds.
3. Then attach the wake stream (step 4b) — the seat must be decided
   first, because the bridge's watcher claims the watch under the seat it
   resolves at attach time (it re-reads the seat file every poll, so a
   runtime seat is followed without any re-arm).

Memory scoping does not change: co-workers still share one project memory.
Only addressing splits. If the session was launched with
`ENGRAM_INBOX_IDENTITY` already set, a launcher seated you — keep that seat
and skip this step.

**⛔ SEAT COLLISION banner at startup? Do NOT flee your seat by reflex.**
Within ~5 minutes of your startup (or a bridge restart), a collision flag is
usually your predecessor's dying tail — its bridge outlives its goodbye and
keeps beating briefly. The server treats displaced predecessors as corpses
and the flag self-clears; re-check on your next call before acting. Only a
flag that PERSISTS is a real rival (two live sessions on one declared name)
— then, and only then, take a role-suffixed runtime seat via
`memory_take_seat` (no relaunch). Your project and lane addresses keep
listening for you either way; the incarnation ordinal is mortal by design,
so moving it loses nothing durable — but moving it for a corpse costs your
successor the address's thread continuity for no reason.

## 4b. Attach the Wake Stream (always-listen) — you NEVER start a watcher

**Engram's bridge owns the inbox watcher.** It spawned one for this session
the moment you made your first memory call, and supervises it (respawn on
death, one claim per seat). No agent starts, re-arms, or "fixes" a watcher —
the `engram-inbox-wait` launch ritual is RETIRED (owner order 2026-08-21,
after a night of deaf sessions that had each armed their own).

Your ONE act is to attach a reader to the stream the bridge opened:

1. Call `memory_status`. Read the `wake stream:` line.
2. If it says `NOT COVERED … attach with Monitor (persistent) -> while true;
   do cat <fifo> 2>/dev/null; sleep 1; done` — run **exactly that command**
   under the **Monitor** tool with `persistent: true`. Then stop.
3. If it says `COVERED (… reader attached …)` — a reader is already on the
   stream; do nothing.
4. Verify: `memory_status` reads `COVERED` within ~10s of the attach. Until
   then every memory tool result carries a `⛔ WAKE STREAM NOT COVERED`
   banner with the same command — that banner, not prose, is the standing
   instruction; it clears itself once you are attached.

Hard rules (each one was a real deaf session):
- **Never `tail -F`/`tail -f` the FIFO** — tail buffers a FIFO until
  writer-EOF, which never comes; the seat reads `covered` and you are deaf.
- **Never launch `engram-inbox-wait`** (any flags, any path). It does not
  claim, the bridge's watcher stays unattached, the seat reads `unheld` —
  and two processes then race for one seat.
- **Never run the cat-loop in a foreground Bash** — it does not return; it
  locks your turn until the tool times out.
- **Never WAIT for coverage in a foreground Bash either** (no `sleep N`,
  no `for i in …; do sleep 10; done`, no `until … COVERED`). If
  `memory_status` still reads `NOT COVERED` after one re-check (~10s),
  read the watcher log line it prints (`inbox-wait: watch held by … —
  re-claiming in Ns` means a predecessor's claim is draining and the bridge
  re-claims ON ITS OWN), say so in your summary, and **return the turn**.
  A session sleeping in a Bash is not idle — the owner cannot talk to it;
  2026-08-21 an AB session blocked 170s this way and read as "stuck".
  If you must be told when it flips, use Bash `run_in_background` with an
  `until` loop — never the foreground.
- If the banner reappears mid-session, your Monitor reader died (it
  happens): re-attach with the same command. The watcher survives the
  detach and re-sends the wake it lost.
- If `memory_status` says `bridge watcher not started`, the bridge has the
  kill-switch set or predates watch-claim — REPORT it (inbox to `engram`),
  do not hand-arm.

What the stream carries: one JSON line per wake. On attach it emits a
`backlog-digest` (counts of unread pre-arm mail on your addresses) and an
`estate-survey`; an unread `authority-directive` older than the watcher
still gets its own line. Treat those as live instructions, not history —
they arrived while nobody was listening. Corollary for step 4: **ack
directive mail only when you have actually handled it.**

## 5. Project Identity

The project `CLAUDE.md` (repo root) is already in context — do not re-read it. Read `BACKLOG.md` (this IS required every session; `head -60` plus the section headers is enough to orient, read an item in full only when you pull it). For migration work, also check `docs/project-migration.md`.

## 6. Check Current State

```bash
git status
git log --oneline -10
```

## 7. Summarize and Ask

- Lead with the most recent state (from memory sweep), not just the handoff
- If the handoff was incomplete, say so and explain what you reconstructed
- Identify pending tasks, blockers, next steps
- Note any inbox messages needing attention
- Ask what we're working on today
