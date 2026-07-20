# The backlog standard: a ledger you could publish, a journal you never do

**Status:** ADOPTED 2026-07-20 · applies to every project; engram is the
reference implementation.

## The doctrine this serves

**Assume any repo might slip public.** Visibility is one `repo edit` or one
stray OAuth grant away from changing — so privacy is a mitigation, never a
license. Every git-tracked file is written as if strangers will read it:
no secrets, no personal information, no client identities, no network
topology, no exploit detail, no operational diary. A repo should read like a
project, not a war room.

## The failure this prevents (learned the hard way)

A tracked `BACKLOG.md` that keeps completed items becomes an **operational
journal wearing a todo list's clothes**. Audited across one real fleet of 13
projects, backlog files ran 55–86% completed-item history — and that history
is exactly where the leaks lived: personal names and phone numbers, client
relationships, internal hostnames and addresses, fixed-vulnerability
writeups, candid strategy. None of it was needed to track *open* work.

The failure has two halves, and they need opposite tools:

| Half | What it is | Where it belongs |
|---|---|---|
| **Forward ledger** | Open work only — terse, actionable | Git-tracked `BACKLOG.md` (public-safe by construction) |
| **Backward journal** | Done items, postmortems, decisions, lessons, vuln detail, strategy | Persistent memory (engram), private by default |

Mixing them is what turns a 40-line todo list into a 150KB liability.

## The ledger: `BACKLOG.md` format

```markdown
# <project> — BACKLOG (open items only)

> Open work only. Done = delete the line (its story lives in the commit and
> in memory). No secrets, PII, client names, topology, or exploit detail —
> this file is written as if public. Journal → engram memory.

## Now (blocking or next up)
- **X-1** Fix inbox pagination under small limits. (found 2026-07-19)

## Next (committed, not started)
- **X-2** Single provider token: partition-read + inbox-send in one grant.

## Later / decide
- **X-3** Driver productization — needs presence v2 first.
```

Rules, each load-bearing:

1. **Open items only.** The moment something is done, **delete the line** —
   in the same commit as the fix when possible. Its history lives in git
   (the commit message references the ID) and its *why* lives in memory.
   No FIXED section, ever. A completed item retained "for reference" is the
   first stone of the next journal.
2. **Terse.** ID + one to three lines: what, why it matters, what unblocks
   it. If an item needs paragraphs, the paragraphs go in memory
   (`backlog/<ID>` key) and the ledger line links the ID.
3. **Public-safe by construction.** Security items are tracked by *task*,
   never by *recipe*: "harden default bind posture" — not the config lines
   that exploit it. Repro detail for open vulnerabilities lives in memory
   until shipped. Client work gets neutral labels.
4. **IDs are stable** (`X-1`, `SEC-2`, …) so commits and messages can
   reference them (`Fix X-1: …`).
5. **One ledger per repo, at the root.** If it's not in the ledger, it's not
   tracked. (The *content* rule changed; the single-source-of-truth rule did
   not.)

## The journal: engram memory

Everything the old journals held still gets captured — durably, privately,
searchably — as memory instead of git:

| What | Key convention | Scope |
|---|---|---|
| Completed-item story / postmortem | `fix/<id-or-slug>` | project |
| Decision + rationale | `decision/<slug>` | project (or shared) |
| Lesson that transfers | `lesson/<slug>` | shared |
| Long detail behind a ledger line | `backlog/<ID>` | project |
| Session narrative / handoff | `session/<date>-<slug>`, `startup/next` | project |
| Open-vuln repro detail (until fixed) | `vuln/<ID>` | project |

The journal is *richer* than the old files — no public-safety constraint
applies there — and it's queryable by meaning, which a 150KB markdown
graveyard never was.

## Wiring (structure beats discipline)

Discipline decays; wiring doesn't. The standard is only real if the tools
enforce it:

- **Session start** (startup routine): read `BACKLOG.md`; when idle, pull the
  top OPEN item.
- **On completion** (the commit itself): delete the ledger line in the same
  change that fixes it; write `fix/<id>` to memory if the story is worth
  keeping.
- **Session end** (wrapup routine): sweep the ledger — anything shipped this
  session gets its line deleted and its story journaled; anything discovered
  gets a line added. The wrapup step is *mandatory*, not best-effort.
- **Hygiene lint** (`scripts/repo-hygiene-check.sh`): greps tracked files for
  never-in-git patterns (key material, private-range and overlay IPs, phone
  numbers, personal emails, plus a local gitignored denylist for names).
  Run it in CI or pre-commit on any repo that could ever go public — and per
  the doctrine, that's all of them.

## Migrating an existing journal-backlog

1. Write the new lean ledger from the OPEN items only, rephrased public-safe.
2. Archive the old file's full content to memory (`backlog/archive-<date>`)
   — nothing is lost, it just stops being tracked in git.
3. Replace the file; if the old one contained sensitive history, scrub git
   history too (filter-repo) — deleting HEAD content does not unpublish the
   past.
4. Wire the startup/wrapup hooks before calling it done: an unwired standard
   is a suggestion.
