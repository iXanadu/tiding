# OAuth front door for hosted agents — scope

Status: SCOPED 2026-10-21, not approved to build. Owner asked for the scope
after two hosted agents (one on OpenAI's platform, one on Meta's) both
preferred an OAuth connection to carrying a bearer token.

## Why this exists

Engram's MCP is the standard *local* kind: a stdio bridge running beside the
agent. A vendor-hosted agent cannot start a local process; it can only reach a
*remote* MCP server over HTTPS, and the MCP authorization model for remote
servers is OAuth 2.1. OpenAI's connectors accept OAuth or no auth, never a
pasted static key. Nothing about engram's MCP is non-standard; hosted
platforms simply need a different door.

## Shape

A separate front-door service. Engram's own auth, routes and bridges are not
touched; if the front door fails, only agents that log in through it lose
memory.

```
hosted agent ──OAuth + MCP (HTTPS)──▶ front door ──REST + bearer──▶ engram
                                      │ holds one restricted principal
                                      │ token per approved client
                                      └ owner approves each client once
```

Components:

1. **Discovery.** Protected Resource Metadata (RFC 9728) on the MCP URL,
   naming the authorization server; Authorization Server Metadata
   (RFC 8414).
2. **Client registration.** Client ID Metadata Documents (the current MCP
   spec's preferred method, and OpenAI's) with Dynamic Client Registration
   kept as a fallback (still used by ChatGPT when CIMD is not offered; Meta's
   behaviour unverified).
3. **Authorize + consent.** Authorization code with PKCE (S256). The consent
   page sits behind the owner's own login; the owner picks which engram
   principal the client maps to. No public sign-up — an unapproved client
   gets nothing.
4. **Tokens.** Short-lived access tokens, refresh tokens, revocation,
   `iss` in authorization responses (RFC 9207). Stored server-side.
5. **MCP endpoint** (Streamable HTTP) exposing the REST surface already
   documented in [remote-agents.md](../remote-agents.md): whoami, search,
   get, set, keys, inbox, send, ack, resolve, roster. Each call goes to
   engram with the mapped principal's token, so namespace enforcement stays
   where it is today.
6. **Waking** is out of scope: OAuth/MCP does not push. Hosted agents keep
   the per-address mail signal.

## Hosting choice

| | Cloudflare Worker | Self-hosted Python service |
|---|---|---|
| OAuth plumbing | vendor library (OAuth provider + remote-MCP template) | Authlib + MCP Python SDK, wired by us |
| New exposure | none (public hostname already fronted there) | one more public service on an existing box |
| Failure domain | adds nothing for agents already reaching engram via the public hostname | adds that box |
| Effort | ~2 days | ~2.5–3 days |

Recommendation: Cloudflare Worker, unless the owner prefers to keep it on
his own boxes. Either way the design above is identical.

## Security requirements (gate to ship)

- One restricted principal per client; never the owner's or an admin token.
  Mail is admitted by fleet READ, so read scope is chosen deliberately.
- Redirect URIs allow-listed per vendor; PKCE mandatory; refresh rotation.
- Every grant, refresh and revocation audited; a kill switch that revokes
  all front-door grants in one step.
- Rate limit per client; request bodies capped as engram already caps them.
- Independent adversarial review before any client is approved.

## Plan

1. Spike (½ day): discovery + CIMD/DCR + consent against the MCP Inspector.
   Confirms the library supports CIMD; if not, DCR-only for the first cut.
2. Tools (1 day): the ten REST-backed tools, tested against the test
   instance, then prod with a throwaway principal.
3. Security pass + review (½–1 day).
4. Clients: ChatGPT developer mode first (best-documented), then Muse.
   Budget one debugging round per vendor.

## Unknowns to settle in the spike

- Whether Meta's consumer app supports remote-MCP custom connectors with
  OAuth (third-party guides say yes; Meta's own documentation is not
  confirmed).
- CIMD support in the chosen OAuth library.
- Which ChatGPT plan/mode is needed to add a custom connector.

## Team

Not a team-sized job. One builder (the engram PM) plus one independent
security reviewer on a different model before go-live; the host admin only
for the DNS/route.
