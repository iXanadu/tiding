# Front door: OAuth + remote MCP for hosted agents

A Cloudflare Worker that lets a vendor-hosted agent (one that can't run a
local bridge or carry a bearer token) connect to Tiding as a remote MCP
server, using OAuth. Design and security gate:
[docs/design/oauth-front-door.md](../docs/design/oauth-front-door.md).

Status: built, not deployed. The local end-to-end check passes (22 checks).

Refused before consent: a callback host not in `REDIRECT_HOSTS` (each
vendor's callback host; loopback only in local dev), and any request
without S256 PKCE, confidential clients included. Every grant carries
exactly the `mcp` scope, set by the door; the MCP handler refuses a token
without it.

Sign-in: the owner approves each client on the consent page with a long
random passphrase kept in his password manager (5 attempts a minute per IP).
No third-party identity provider to register or maintain. Kill switches:
`ENABLED` var (anything but `true` → 503 everywhere) and
`POST /frontdoor/revoke-all` with the `KILL_TOKEN` bearer. Audit lines
(`{"audit": …}`) go to Workers Logs; every forwarded call also lands in the
memory server's own request log under the mapped principal.

On the edge, single-use codes and the grant list live in KV, which is
eventually consistent: a replayed code can succeed within the cache window
(PKCE still demands the verifier), and revoke-all can miss a grant made in
the last minute. The authoritative kill is on the memory server: deactivate
the mapped principal, and every call the front door makes for it fails.

```bash
npm ci && npx wrangler types
# .dev.vars (mode 600, never committed):
#   TOKEN_<PRINCIPAL>=<that principal's token>   one per mappable principal
#   OWNER_PASSPHRASE=<long random secret>
#   KILL_TOKEN=<long random secret>
npx wrangler dev --port 8787
python3 test/roundtrip.py        # DCR, PKCE, consent, MCP, refresh, revoke, kill
```

## Runbook

- **Emergency stop:** deactivate the mapped principal on the memory server
  (`PATCH /admin/principals/<name>` with `{"active": false}`). The next call
  the front door makes for it fails; the server re-reads `active` on every
  token lookup. Then `POST /frontdoor/revoke-all` and set `ENABLED` to
  anything but `true`.
- **Reactivating is not a clean slate.** KV may still hold refresh tokens
  that revoke-all missed (30-day idle lifetime). Before reactivating, run
  revoke-all again after the KV window (about a minute), or rotate the
  principal's token and update its `TOKEN_<NAME>` secret.
- **Adding a vendor:** a refused consent shows the callback host the app
  asked for. Add that exact host to `REDIRECT_HOSTS` and redeploy.

Review: independent cross-model PASS at 461e77e (2026-10-28). Residuals
accepted: the allow-list is per host, not per full URI; the library can
send its own error redirect (no code) before the host check runs; refresh
and the library's revocation endpoint aren't audited; the passphrase limit
is per IP.
