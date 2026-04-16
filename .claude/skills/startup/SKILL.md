---
name: startup
description: When a new session begins in this project, run this to orient on project state, memory, and recent work before doing anything else.
---

Session startup. Follow these steps:

## 0. Orientation Prelude

Do both of these before any `scope=project` memory calls or substantive work.

**0a. Refresh global directives.** Read `~/.claude/CLAUDE.md` — your global operating rules (communication style, memory scope resolution, git safety, Python convention). It's loaded at session start but fades when buried in a long system prompt; re-reading pulls it into active attention.

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

## 3. Thorough Memory Recovery

The handoff captures one moment. To orient fully, sweep memory with several targeted queries (run in parallel where possible):

### Project scope
1. `memory_search` query="session" scope=project limit=10 — recent session summaries
2. `memory_search` query="decision architecture" scope=project limit=10 — design decisions
3. `memory_search` query="strategy direction goals" scope=project limit=5 — strategic context
4. `memory_search` query="wip current working module" scope=project limit=5 — active work

### Shared + machine
5. `memory_search` query="engram memory semantic" scope=shared limit=5 — cross-project lessons relevant to engram
6. `memory_search` scope=machine limit=3 — local env, paths, services

### Reconcile
Compare dates across results. If the most recent memories are newer than `startup/next`, the handoff is incomplete — note the gap and lead with the newer state.

## 4. Check Inbox

`memory_inbox` — messages from other Claude instances. Read and reply to anything relevant or actionable.

## 5. Read Project Identity

Read `.claude/CLAUDE.md` for structure, conventions, and commands. For migration work, also check `docs/project-migration.md` and `docs/commands-to-skills-migration.md`.

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
