# engram — BACKLOG (open items only)

> Open work only. Done = delete the line (its story lives in the commit and in
> memory). No secrets, PII, client names, topology, or exploit detail — this
> file is written as if public. Journal → engram memory. Standard:
> [docs/backlog-standard.md](docs/backlog-standard.md).

## Now (blocking or next up)

- **REL-1** Public-release gate: history rewrite push + verification sweep
  (`scripts/repo-hygiene-check.sh` clean on tree AND full history), publish
  checklist review. Repo stays private until this closes. **Blocks on
  SEC-AUDIT and DOCS-SANITIZE below.**
- **SEC-AUDIT** Remaining items from the 2026-07-21 security audit (the
  blocking code fixes shipped; these are the follow-ups). All verified,
  detail in `lesson/security-audit-2026-07-21`:
  - Gate `/admin/*` + principal-CRUD behind a token even when
    `require_auth=false` on a non-loopback bind (anonymous-admin hole).
  - Vendor Alpine (pinned) + build Tailwind locally; add CSP; move the
    dashboard admin token out of `localStorage` — kills the no-SRI CDN
    supply-chain path to full admin.
  - Watcher: exit/signal on 401/403 instead of retry-forever (fail-open =
    silently missed wakes); non-localhost `http://` TLS warning.
  - `get_principal_by_token` full-bcrypt-scan (auth-spray DoS) — indexed
    lookup; bcrypt 72-byte pre-hash; `remove_alias` path-scoping.
  - systemd hardening directives; log rotation; dedicated service user;
    pin embed-model revision.
- **DOCS-SANITIZE** Three docs are unpublishable internal memos (real names,
  private projects, fleet hosts): `docs/webapp-integration-spec.md`,
  `docs/webapp-native-app-identity.md`, `docs/design/messaging-architecture.md`.
  Sanitize or move to a private location before public. Also: README stale
  (missing `memory_roster`/`memory_resolve` tools, `/memory/{resolve,wait,
  presence,roster}` endpoints, several `ENGRAM_*` env vars); getting-started
  teaches the pre-Phase-4 3-dimension model; provider-credentials-vs-README
  contradiction on `memory_namespace`.
- **IB-6** `unread_only` inbox filter can miss unacked mail under small
  limits — reproduce against a real DB before patching (suspected remnant of
  the fixed newest-N windowing bug).

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
