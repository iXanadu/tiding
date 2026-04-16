Read the following to get up to speed on this project:

0. **Project identity check (MUST run before any `scope=project` memory calls):**
   - Look for `.engram.cfg` at the repo root.
   - If present: parse `project = <name>` — this is the canonical identifier used for all `scope=project` memory and inbox addressing. Proceed.
   - If absent, run this flow:
     1. Derive a candidate name:
        - If CWD matches `~/projects/<name>/`, candidate = `<name>` (unless `<name>` is a generic deploy label like `prod`, `dev`, `staging`, `main`, `trunk`, `current`, `release`, `live` — then skip to step 2).
        - Else `git config --get remote.origin.url` → repo basename without `.git`.
        - Else (admin-style CWD or no match): ask the user for a name, or confirm `admin`.
     2. Show the candidate to the user and ask for confirmation before writing.
     3. On confirmation: write `.engram.cfg` at the repo root containing `project = <name>`, stage it, and commit (message: `Add .engram.cfg — canonical project identifier`).
   - Rationale: basename-only scoping breaks on server layouts like `/var/www/site.com/prod` — every project collapses into the same `prod`/`dev` buckets. `.engram.cfg` is git-tracked so every clone on every host agrees on the name.

1. Search persistent memory for prior context:
   - `memory_search` with query "engram project state" at `scope=project` — session history, decisions, WIP
   - `memory_search` with query "engram" at `scope=shared` — cross-project lessons relevant here
   - `memory_search` with query "engram" at `scope=machine` — local env quirks, paths, services

2. Read `.claude/CLAUDE.md` — project identity, structure, conventions

3. Check git status and recent commits for context on latest changes

After reading, summarize:
- Current project state (from memory)
- Recent work (from git log)
- Any pending tasks or known issues
- Ask what to work on today
