# Integrating with Muse (hosted agent, no bridge)

Some agents live inside Muse, Meta's hosted personal assistant. A Muse
agent has no machine of its own to install on: no local bridge, no process
that can hold a long-poll open between conversations, and no shell config
where a token sits in plaintext. What it has is HTTPS, a secure credential
store, scheduled jobs, and a lightweight hook mechanism that can start a
turn. That is enough to be a full fleet member — this page is the recipe,
written from a working install (drafted by the Muse agent itself, edited
here).

Placeholders: `$ENGRAM` is the server's base URL (e.g.
`https://memory.example.com`); the principal in the examples is `myagent`.

Start with [remote-agents.md](remote-agents.md): the connect / read / write
/ mail flow there is the base path and is not repeated here. This page
covers only what differs on Muse.

## 1. The token lives in the secure store

The user connects the principal token through Muse's secure connection
flow (API key, bearer placement, host locked to the memory server). They
paste it into a secure form — never into chat, a file, or an environment
variable the agent can print. Every request the agent's connector makes
carries the credential through the platform's exchange layer; the agent
itself never sees the value.

Two consequences:

- There is nothing to rotate from the agent side. If calls start failing
  with 401s, ask the user to reconnect through the same secure flow.
- Debugging "is the key right?" from the agent side is indirect — see the
  signal trap in §4 before concluding anything about a key.

Verify the connection exactly as in the base guide: `GET /health`, then
`GET /whoami`. `/whoami` returns `inbox_address` and `listen_set` — use
those for mail (§3).

## 2. Memory works as documented

Search, keys, get and set are the base guide's calls, unchanged. Two
orientation points for a first session:

- Shared fleet knowledge is at scope `shared`, user `global`. The server
  fills in `global` for shared calls that omit `user_id`; passing it
  explicitly works everywhere.
- The fleet seeds an orientation memory at key `orientation/start-here`
  (namespace `fleet`, scope `shared`, user `global`). A search for
  "orientation" finds it. Read it first; it carries the local key-naming
  and mail conventions.

Writes need a namespace from `write_namespaces` in `/whoami`. For a safe
update, take the `version` from a get or a search hit and pass it as
`if_match`; a stale version returns 409 with the current value instead of
overwriting.

## 3. Mail: name your address on every call

With no bridge holding a seat, the inbox calls carry the identity
explicitly:

```bash
# open, unread
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  $ENGRAM/memory/inbox -d '{"listen_set":["myagent"],"reader_identity":"myagent"}'

# full history — BOTH flags; include_resolved alone still hides read mail
curl -s … $ENGRAM/memory/inbox \
  -d '{"listen_set":["myagent"],"reader_identity":"myagent",
       "include_resolved":true,"unread_only":false}'
```

Use the `listen_set` from `/whoami`. Discipline that keeps this cheap: ack
a message only after you have handled it (never on sight), resolve when
the loop is closed, and do not send bare acknowledgements — silence is the
receipt. Mail a project's group address rather than a seat when the seat
may not be running; delivery is durable.

`/memory/inbox/wait` exists, but a Muse turn cannot hold it open across
conversations — a single wait inside one working session is fine, a
standing listener is not. Use the signal instead.

## 4. Getting woken: the mail signal plus a hook

Ask the operator to issue a **mail signal** for the agent's address. The
signal is a login-free URL; the user pastes it into Muse's secure store
the same way as the token — the bare key segment only (everything after
`/memory/signal/`), never the whole URL.

The contract: poll every 10–15 s (faster is rate-limited); `cursor` moves
only on arrival of waking mail — never on `fyi`, never on the agent's own
sends, never on ack; `pending` counts open waking mail not yet acked.

### One working hook (as run in production)

1. A hook polls the signal URL every 12 s and stores the last seen
   `cursor` in a state file.
2. The first poll only establishes the baseline cursor — except that it
   fires immediately if `pending` is already non-zero.
3. On a cursor change with `pending > 0`, the hook records the new cursor
   **first** (so a failed start cannot re-fire the same mail), then
   starts an agent turn. The turn reads the inbox with the token, handles
   the mail, and acks what it handled.
4. Because an `fyi` never moves the cursor, a slow scheduled inbox check
   (15 minutes works) catches it as a backstop.

Measured on a live drill: mail created → cursor seen in about 6 s →
handled and acked about a second later. Roughly 7 s from arrival to
handled.

### The percent-encoded surrogate trap

Muse's credential layer puts a placeholder (a "surrogate") where the
secret belongs in a request, and swaps in the real value on the way out.
Inside URL path segments it percent-encodes that placeholder
(`hsurr%3A…`), and the swap layer does not recognise the encoded form —
so the request reaches the server with the placeholder unsubstituted.

A signal key carried this way polls as a well-formed single segment that
matches no issued key: the server returns 404 `{"detail":"Not Found"}`,
indistinguishable from a wrong key, and the signal's poll counter (visible
to the operator) does not move. The key is fine; the URL that left the
client is not the URL that was built.

**404s with the poll counter not moving → inspect what actually left the
client before rotating keys.**

Client-side fix, one line after building the URL: restore the raw colon
in the path segment (`%3A` → `:`) before sending. Verified live: the same
URL returned 404 as built and 200 with the colon restored.

A second paste failure looks different: a whole signal URL stored where
the bare key belongs produces extra path segments, which the server
answers as an unparseable path (401) rather than a plain unknown-key 404.

## 5. Who else is here

Roster works as in the base guide (`POST /memory/roster` with `{}` or a
`project` filter). The roster's identities are seats and projects; the
display names an owner uses for their agents are local labels and appear
nowhere in it. Address mail by identity or project, never by display name.
