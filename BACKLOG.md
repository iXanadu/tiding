# engram — BACKLOG (open items only)

> Open work only. Done = delete the line (its story lives in the commit and in
> memory). No secrets, PII, client names, topology, or exploit detail — this
> file is written as if public. Journal → engram memory. Standard:
> [docs/backlog-standard.md](docs/backlog-standard.md).

## Now (blocking or next up)

- **REL-1** Public-release gate — **rewrite + sweep DONE 2026-07-21**
  (history rewritten and force-pushed; hygiene patterns clean across every
  blob in every commit; all clones re-pointed). Remaining: publish
  checklist review + the visibility flip itself (operator's click).

## Next (committed, not started)

- **HUD-1** Private multi-party threads (group-reply): a fan-out send
  (`to: [a,b,c]`) records its participant set; `memory_reply` on such a
  thread fans out to ALL participants (not just the sender), each reply
  under the replier's own verified stamp. Kills the AB-relay workaround
  (owner-stamped relayed messages, single point of relay, poll latency).
  Metadata-only — no schema change. Requested by agentbeast 2026-07-21;
  design accepted (rejected alt: runtime channel subscribe — fights
  launch-time membership + watcher re-arm).

## Blocked-external

- **DOCKER-1** Verify the full-stack compose path (build, health,
  store/search roundtrip), then promote it from "experimental" in
  README/deployment docs. Blocked: NO healthy Docker runtime exists on the
  fleet (surveyed 2026-07-21 — Linux spokes have no Docker; macmini's
  OrbStack hangs on daemon start). Needs a runtime repair or a fresh box
  first; the stack definition itself is unproven, not suspect.

## Later / decide

