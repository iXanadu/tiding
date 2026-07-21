# Security model

engram is a **single-operator** memory + messaging service: one person (or one
trusted automation) runs an instance, and the agents they run share it. The
security model is built for exactly that. Read this before exposing an instance
to anything you don't fully control.

## The one invariant: the namespace is the security boundary

A **namespace** is the unit of access control. A principal's token grants
**read** and/or **write** on a set of namespaces, and that is the *only*
confidentiality/authorization wall.

Within a namespace a token can reach, it is deliberately powerful:

- **Memory:** `user_id`, `scope`, and `project` are *organizational* filters,
  not security boundaries. A token that can read namespace `N` can read every
  memory in `N` — all users, all scopes, all projects.
- **Inbox:** any token with access to the inbox namespace can read any
  `listen_set` (i.e. any session's or channel's mail), and ack / resolve /
  archive messages. `from_` is a self-asserted label. **Only `authority` and
  `from_principal` are server-verified** (stamped from the authenticated token,
  unforgeable by a client) — that is what makes owner broadcasts trustworthy.
- **Presence:** heartbeats are self-reported. Any token can report presence for
  any identity. The roster is cooperative liveness data, not an authenticated
  claim; the seat-collision detector flags accidental clashes, not attacks.

**Consequence — the rule that follows from the invariant:** never hand a
namespace token to a party you would not trust with everything in that
namespace. There is no intra-namespace isolation to fall back on. Separate
trust domains get separate namespaces (or separate instances).

This is a deliberate design for cooperating agents under one operator, not an
oversight. If you need mutually-distrusting tenants in one namespace, engram is
not the right tool as-is.

## Network posture: secure by default

- **Loopback by default.** `ENGRAM_HOST=127.0.0.1`. Nothing off-box can reach
  it; tokenless local use is fine.
- **A non-loopback bind without auth refuses to start.** Binding `0.0.0.0`
  with no auth requires the explicit `ENGRAM_ALLOW_INSECURE_BIND=true`
  opt-out — intended only for a genuinely private overlay network (Tailscale,
  WireGuard). The guard checks the address the server *actually* binds (the
  service launches via `python -m server`, which binds `ENGRAM_HOST`).
- **Host-header allowlist.** `ENGRAM_TRUSTED_HOSTS` (default
  `localhost,127.0.0.1,[::1]`) blocks DNS-rebinding: a malicious web page can't
  drive your loopback instance by rebinding its DNS. Add your hostname /
  Tailscale name when binding non-loopback.
- **CORS** permits only `https://claude.ai` (for the web bridge), credentials
  off.

### To expose an instance on a network, pick one

| Posture | Config | When |
|---|---|---|
| Loopback (default) | `ENGRAM_HOST=127.0.0.1` | Local single box |
| Authenticated | `ENGRAM_REQUIRE_AUTH=true` + principal tokens + `ENGRAM_TRUSTED_HOSTS` | Any real network |
| Private overlay, tokenless | `ENGRAM_HOST=0.0.0.0` + `ENGRAM_ALLOW_INSECURE_BIND=true` + `ENGRAM_TRUSTED_HOSTS` | Tailscale/WireGuard among trusted machines |
| **Public, tokenless** | — | **Never.** |

## Authentication

- `require_auth=false` (default): local trust. Anonymous callers are allowed;
  `check_namespace_access` is a no-op. Admin endpoints (`/admin/*`, principal
  CRUD) are reachable — acceptable *only* because the default posture is
  loopback. **If you bind to a network, set `require_auth=true`.** (A future
  hardening will gate admin/principal-CRUD behind a token even in this mode;
  tracked in the backlog.)
- `require_auth=true`: every request needs a valid principal Bearer token;
  admin endpoints require an `is_admin` principal. Tokens are `engram_<random>`,
  bcrypt-hashed at rest, shown once at creation.

## Secrets

- Tokens live in `~/.config/engram/identities/<name>` (0600) or provider-global
  config — **never in a repo**. `.env` is chmod 600 by the installer.
- The embedding model loads with `trust_remote_code=True` (required by
  nomic-embed); `ENGRAM_EMBED_MODEL` is operator-controlled, so treat a model
  swap as running that repo's code — pin a model you trust.

## Untrusted message content

Inbox message bodies are attacker-influenceable and flow into reading agents'
context. The bridge **fences** every message body as data (not instructions)
and neutralizes content that mimics engram's own verified-owner framing, so a
hostile peer can't counterfeit an owner directive. The verified badge is
rendered only from the server-stamped `authority` field.

## Reporting

This is early-stage software under active hardening. If you find a
vulnerability, open a GitHub issue (or private advisory) — please describe the
trust-model assumptions it breaks, since many "findings" inside a namespace are
by-design per the invariant above.
