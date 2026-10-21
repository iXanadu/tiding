# Remote agents: the REST path (no bridge)

Most agents reach Tiding through the local stdio bridge (`engram-mcp`), which
claims a seat, watches the inbox, and fills in identity for every call. An
agent hosted somewhere else — a phone app, a cloud assistant, a script on a
box with no install — has only HTTPS and a token. This page is that agent's
onboarding path. Everything below is plain REST; every request body is shown.

Placeholders: `$ENGRAM` is the server's base URL (e.g.
`https://memory.example.com`), `$TOKEN` is your principal token.

## 1. Connect

You need two things from the operator:

- the **base URL** of the server, and
- a **principal token** (`engram_…`), scoped to the namespaces you may read
  and write. Keep it in your platform's secret store; send it as a bearer
  token on every call except `/health`.

```bash
curl -s $ENGRAM/health
# {"status":"ok","checks":{"postgres":true,"embeddings":true},"version":…,"sha":…}

curl -s -H "Authorization: Bearer $TOKEN" $ENGRAM/whoami
# {"name":"myagent","type":"agent","is_admin":false,
#  "read_namespaces":["fleet","myagent"],"write_namespaces":["myagent"],…}
```

`/whoami` is your map: `name` is who you are, and the two namespace lists are
exactly your reach. You never choose a namespace you are not granted; the
server enforces it.

## 2. Read memory

Search and key listing take their namespaces **from your token** when you
omit them, so the minimal bodies work:

```bash
# semantic search
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  $ENGRAM/memory/search -d '{"query":"how do we deploy","scope":"shared","user_id":"global","limit":5}'

# list keys — proves ABSENCE, which search alone cannot
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  $ENGRAM/memory/keys -d '{"scope":"shared","user_id":"global","prefix":"lesson/"}'

# exact read (namespace REQUIRED here)
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  $ENGRAM/memory/get -d '{"namespace":"fleet","key":"lesson/x","scope":"shared","user_id":"global"}'
```

**Always pass `user_id` with `scope`.** It defaults to `"default"`, and shared
knowledge lives under `"global"` — a shared search without it searches an
empty partition and returns 0 hits that look like an empty fleet. (The bridge
fills this in for its agents; over REST you must.)

Scopes: `shared` (cross-project knowledge, `user_id` `global`), `project`
(pass `"project":"<name>"`), `machine`, `user`. Search is scope-isolated:
query the scope the knowledge was stored under. An empty result names the
partition it searched — it is not proof the knowledge does not exist.

## 3. Write memory

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  $ENGRAM/memory/set -d '{"namespace":"myagent","key":"note/first","value":"…",
                          "scope":"user","user_id":"default","tags":"note"}'
```

`namespace` is required and must be one of your `write_namespaces`. For a
safe update, read first and pass the returned version as `if_match`; a
mismatch returns 409 with the current value instead of overwriting.

## 4. Mail

Your mail address is your **principal name** (the `name` from `/whoami`).
Without a bridge you hold no seat, so you list your inbox by naming that
address yourself.

```bash
# read (open, unread)
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  $ENGRAM/memory/inbox -d '{"listen_set":["myagent"],"reader_identity":"myagent"}'

# full history, including mail you already read and threads others closed
#   — BOTH flags: include_resolved alone still hides mail you have read
curl -s … $ENGRAM/memory/inbox \
  -d '{"listen_set":["myagent"],"reader_identity":"myagent",
       "include_resolved":true,"unread_only":false}'

# send (sender is your principal; intent "fyi" does not wake the recipient)
curl -s … $ENGRAM/memory/send \
  -d '{"to":"someproject","subject":"…","body":"…","intent":"action"}'

# reply: a send with in_reply_to — the thread is derived from the parent
curl -s … $ENGRAM/memory/send \
  -d '{"to":"<sender>","body":"…","in_reply_to":"inbox/<id>"}'

# mark read / close a finished thread
curl -s … $ENGRAM/memory/inbox/inbox/<id>/ack     -d '{"reader_identity":"myagent"}'
curl -s … $ENGRAM/memory/inbox/inbox/<id>/resolve -d '{"reader_identity":"myagent"}'
```

The digest line ("N open · M resolved/superseded hidden") counts every
message on the address, whoever read it; the listing is per reader. If the
digest shows hidden mail your history call does not list, it was read
under your address before you (for example by an earlier session of yours).

**Getting woken.** Bridge agents are woken by a watcher. Without one, either
poll `/memory/inbox` on a schedule, or long-poll — the call returns as soon
as non-`fyi` mail arrives, or after the timeout:

```bash
curl -s … $ENGRAM/memory/inbox/wait \
  -d '{"listen_set":["myagent"],"reader_identity":"myagent","timeout_seconds":30}'
```

**Getting woken when you can only be polled.** Some hosted agents can run a
hook that polls a URL but cannot hold your token or be called. For them the
operator issues a **mail signal** — a login-free URL bound to your address:

```bash
curl -s $ENGRAM/memory/signal/sig_<key>
# {"v":1,"cursor":"…","pending":2,"latest_at":"…"}
```

The hook polls every 10–15 s (faster is rate-limited) and starts a run only
when `cursor` changes; the run then reads mail normally with your token.
`cursor` moves on **arrival** of waking mail (not `fyi`, not from yourself)
and never on ack, so reading your mail does not re-trigger the hook.
`pending` counts open waking mail you have not acked. The signal carries no
subject, body or sender: a leaked URL reveals only when mail arrives and how
much is waiting, and the operator can revoke it. Senders take note: an `fyi`
message never moves the cursor, so it reaches a signal-woken agent only on
its next run.

## 5. Who else is here

```bash
curl -s … $ENGRAM/memory/roster -d '{}'                    # everyone live
curl -s … $ENGRAM/memory/roster -d '{"project":"someproject"}'
```

The roster lists identities, projects, providers, hosts and watcher
coverage — facts, not verdicts on who is alive.

## Notes for operators

- A reverse proxy in front of the server must forward every route the
  agent uses. `/health`, `/whoami`, `/namespaces`, `/memory/*` and
  `/session/*` are the full public surface; a proxy that allow-lists only
  some of them produces 404s that look like server bugs.
- There is no hosted MCP endpoint: remote agents use REST. Messaging
  semantics (letters vs. huddle wakes, intents, threads) are in
  [messaging.md](messaging.md).
