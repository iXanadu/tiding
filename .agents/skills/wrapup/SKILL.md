---
name: wrapup
description: When the user is ending a session, says goodbye, or asks to wrap up — run this to document the session's work and persist state to engram memory.
---

End-of-session wrap-up:

0. **BACKLOG sweep (mandatory, not best-effort):** open `BACKLOG.md` —
   delete the line for anything shipped this session (journal its story to
   memory as `fix/<id>` if worth keeping); add a terse public-safe line for
   anything discovered and still open. The ledger holds OPEN items only
   (`docs/backlog-standard.md`). Run `scripts/repo-hygiene-check.sh` if any
   tracked file changed; it must be clean.
0b. **Mail drain (O5 — a graceful end drains its own estate):** call
   `memory_inbox` and take it to ZERO unread, with the right verb per row:
   - **Resolve** every thread whose loop this session closed (answered,
     shipped, decided) — resolve drains it for everyone, so never resolve
     a thread still waiting on someone else.
   - **Ack** what you have read and handled that others still own.
   - **Will the rest:** anything open that needs a successor — an
     unanswered ask, a pending confirmation — gets an explicit disposition
     line in `startup/next` (step 5): what it is, what state it's in, what
     the successor should do. Unread mail with no disposition is an estate
     leak; the 72h sweep and the climb are backstops, not your plan.
1. Store session summary at scope=project: key=session/YYYY-MM-DD-brief-desc
2. Promote any generalizable lessons to scope=shared with lesson/ or fix/ key prefix
3. Commit any uncommitted code changes (stage specific files, not `git add .`)
4. Clean up WIP: `memory_forget` key=wip/current scope=project (if it exists).
   **Stale-memory pass:** while there, supersede or rewrite any project
   memory this session made stale (a plan that changed, a fact now wrong) —
   `memory_supersede` beats leaving two versions for the successor to
   reconcile at startup-sweep ranking.
5. Store a startup message at scope=project: key=startup/next
   - Quick review of where things stand right now
   - What was being worked on, current state (running services, pending issues, etc.)
   - References to specific memory keys worth reviewing
   - What to do next or pick up
   - This is the first thing the next session reads, so make it actionable
6. Brief recap to the user of what was done and what's next
