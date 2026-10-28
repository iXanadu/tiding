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
