# Front door: OAuth + remote MCP for hosted agents

A Cloudflare Worker that lets a vendor-hosted agent (one that can't run a
local bridge or carry a bearer token) connect to Tiding as a remote MCP
server, using OAuth. Design and security gate:
[docs/design/oauth-front-door.md](../docs/design/oauth-front-door.md).

Status: spike. The local end-to-end check passes; it is not deployed.

```bash
npm ci && npx wrangler types
# .dev.vars (mode 600, never committed):
#   TOKEN_<PRINCIPAL>=<that principal's token>   one per mappable principal
#   OWNER_PASSPHRASE=<spike-only sign-in>
npx wrangler dev --port 8787
python3 test/roundtrip.py        # DCR, PKCE, consent, MCP, refresh, revoke
```

The owner's passphrase sign-in is a spike stand-in. Before any client is
approved it is replaced by an upstream identity sign-in.
