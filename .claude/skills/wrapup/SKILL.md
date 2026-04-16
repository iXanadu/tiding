---
name: wrapup
description: When the user is ending a session, says goodbye, or asks to wrap up — run this to document the session's work and persist state to engram memory.
---

End-of-session wrap-up:

1. Store session summary at scope=project: key=session/YYYY-MM-DD-brief-desc
2. Promote any generalizable lessons to scope=shared with lesson/ or fix/ key prefix
3. Commit any uncommitted code changes (stage specific files, not `git add .`)
4. Clean up WIP: `memory_forget` key=wip/current scope=project (if it exists)
5. Store a startup message at scope=project: key=startup/next
   - Quick review of where things stand right now
   - What was being worked on, current state (running services, pending issues, etc.)
   - References to specific memory keys worth reviewing
   - What to do next or pick up
   - This is the first thing the next session reads, so make it actionable
6. Brief recap to the user of what was done and what's next
