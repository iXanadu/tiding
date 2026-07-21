# engram — BACKLOG (open items only)

> Open work only. Done = delete the line (its story lives in the commit and in
> memory). No secrets, PII, client names, topology, or exploit detail — this
> file is written as if public. Journal → engram memory. Standard:
> [docs/backlog-standard.md](docs/backlog-standard.md).

## Now (blocking or next up)

- **REL-1** Public-release gate: history rewrite push + verification sweep
  (`scripts/repo-hygiene-check.sh` clean on tree AND full history), publish
  checklist review. Repo stays private until this closes. **Unblocked** —
  SEC-AUDIT and DOCS-SANITIZE both cleared 2026-07-21; what remains is the
  rewrite itself + the sweep (operator-gated: force-push of rewritten
  history needs Rob's go).

## Next (committed, not started)


## Blocked-external

- **DOCKER-1** Verify the full-stack compose path (build, health,
  store/search roundtrip), then promote it from "experimental" in
  README/deployment docs. Blocked: NO healthy Docker runtime exists on the
  fleet (surveyed 2026-07-21 — Linux spokes have no Docker; macmini's
  OrbStack hangs on daemon start). Needs a runtime repair or a fresh box
  first; the stack definition itself is unproven, not suspect.

## Later / decide

- **LIVE-1** *(handed off 2026-07-21)* Keep-going driver: engram's rail
  (wake + roster + intent + lifecycle) is complete; the driver judgment
  layer is AgentBeast's per the standing division of labor — formally
  handed off via inbox (ack pending). Drops off this ledger once
  agentbeast acknowledges ownership.
