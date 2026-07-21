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
- **IB-6** `unread_only` inbox filter can miss unacked mail under small
  limits — reproduce against a real DB before patching (suspected remnant of
  the fixed newest-N windowing bug).
- **IB-7** Two different sender sessions leaked trailing tool-call XML
  fragments into message bodies (same defect class, same day). Sender-side
  compose bug, but the transport could defensively strip/flag trailing
  tool-XML at send time. Investigate frequency before building.

## Next (committed, not started)

- **NS-2** Retire the legacy namespace alias once every long-lived session
  has restarted on the current bridge (grace period; verify no stragglers in
  the audit log first).
- **DOC-7** Adoption docs pass: human-agent daily workflow guide; "where it
  fits" positioning paragraph (fit, not fight — see `decision/positioning-*`
  in memory).

## Later / decide

- **DOCKER-1** Verify the full-stack compose path on a box with a healthy
  Docker runtime (build, health, store/search roundtrip), then promote it
  from "experimental" in README/deployment docs. Local attempt was blocked
  by runtime issues, not by the stack definition.

- **LIVE-1** Productize the keep-going driver on roster+intent rails
  (advance-at-seam, never interrupt; escalate irreversible gates). Rail is
  live; driver judgment layer is external.
- **APP-1** Per-app inbox identity pattern for multi-app projects — blocked
  on a live second-app validation round-trip.
- **LIFE-2** Inbox lifecycle wave 2 (client-driven resolve/supersede
  ergonomics) — build when inbox noise is a felt problem, not before.
