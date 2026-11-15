# Signal relay — wake an assistant that can only be called

A single-file sidecar (Python standard library only) for hosted assistants
that **cannot poll**. It watches each assistant's
[mail signal](../../docs/remote-agents.md#4-mail) and calls the assistant's
trigger URL (a webhook) when waking mail arrives.

Which assistants need it, and which do not, is laid out in
[docs/assistants.md](../../docs/assistants.md). In short:

| The assistant's platform… | Needs the relay? |
|---|---|
| can run its own poller between conversations | No. It polls the signal itself. |
| can only be started by an outside call (webhook) | **Yes.** |
| can do neither | No. Nothing can wake it; it checks on its own schedule. |

## Why a sidecar

The memory server never makes an outbound call and never holds a vendor
credential. The relay does both, beside the server and not inside it: it can
be stopped, moved or replaced without touching the store, and a problem in it
cannot take mail down.

Run it on a machine that stays awake. The box that runs the memory server is
the natural place: if that box is down no mail arrives anyway, so the relay
adds no new way to fail.

## What it does

1. Polls each target's signal URL (default every 12 s).
2. When the `cursor` has moved **and** mail is `pending`, POSTs the target's
   webhook, then remembers the cursor.
3. A failed call is not remembered: the wake stays owed and is retried with
   backoff (5 s doubling to 10 min) until it succeeds or the mail is read.

What it guarantees, and what it does not:

- **One call per burst.** Successful calls to one target are spaced at
  least `min_interval_seconds` apart (default 60). Mail that arrives inside
  the window rides on the call already made, because the run it started
  reads the whole inbox. A call that gets no answer is retried no sooner
  than that interval either; a call that is refused outright (4xx, 5xx)
  is retried on the shorter backoff.
- **No call when nothing is waiting.** If the assistant already read the
  mail, the cursor is remembered and no call is made.
- **Never a call on `pending` alone.** Unread mail does not re-trigger a
  call. The one exception is `refire_after_seconds`, off by default.
- **At least once, not exactly once.** A call that succeeded but whose
  answer was lost is made again. So is a call after the newest waking
  message is archived or deleted, because the cursor then steps back to a
  value the relay has not called for. Make the assistant's run exit quickly
  when its inbox is empty.
- **`fyi` mail does not wake.** It never moves the cursor. The assistant
  sees it on its next run.

The POST body is a marker and nothing more. A signal carries no subject, body
or sender, so the relay has no mail content to send:

```json
{"source": "tiding-signal-relay", "event": "mail", "target": "hand",
 "pending": 2, "latest_at": "2026-01-01T12:00:00+00:00"}
```

Set `payload` on a target to send a different fixed body, for a platform that
expects its own shape.

## Setup

1. **Issue a mail signal** for the assistant's address (operator, admin token;
   the `/admin` routes are not reachable from the public edge). The
   assistant must already have a login: the call answers 422 unless an
   active principal with that name exists, or you pass `"principal"` to name
   the login the signal belongs to.
   ([docs/assistants.md](../../docs/assistants.md#what-your-user-must-set-up)
   has the principal step.)

   ```bash
   curl -s -X POST "$ENGRAM/admin/signal" -H "Authorization: Bearer $ADMIN" \
     -H 'Content-Type: application/json' -d '{"address":"myassistant"}'
   # → {"path": "/memory/signal/sig_…", …}   shown once
   ```

   The signal URL is the server's base URL plus that path. Use the address
   the assistant's mail arrives on. Issuing a new signal for an address
   revokes the previous one.

2. **Get the assistant's trigger URL** from its platform (often called a
   webhook, automation trigger or routine trigger) and its key, if it has one.

3. **Write the config** at `~/.config/tiding/signal-relay.json`, mode 600.
   Start from [signal-relay.example.json](signal-relay.example.json):

   ```json
   {
     "targets": [
       {
         "name": "hand",
         "signal_url_file": "~/.config/tiding/hand.signal",
         "webhook_url_file": "~/.config/tiding/hand.webhook-url",
         "webhook_bearer_file": "~/.config/tiding/hand.webhook-key"
       }
     ]
   }
   ```

   Each secret can be given inline (`signal_url`, `webhook_url`,
   `webhook_bearer`) or by file (`…_file`). Files keep secrets out of the
   config and let you rotate one without editing it. Every file, and the
   config itself, must be owned by the user the relay runs as and private
   to them (`chmod 600`); the relay refuses to start otherwise. Each secret
   is one value on one line: a file with a comment line or a stray space is
   refused at start, not discovered at the first wake. (On Windows the
   permission check is skipped; protect the files with an ACL.)

4. **Check, then test the call:**

   ```bash
   python3 relay.py --check            # polls each signal once; calls nothing
   python3 relay.py --fire-test hand   # calls hand's webhook once
   ```

   `--fire-test` should start a run on the assistant's side. If it does not,
   the problem is the webhook or its key, not the relay.

5. **Run it as a service.** Templates:
   [launchd](sh.tiding.signal-relay.plist.template),
   [systemd](tiding-signal-relay.service.template). Both restart the relay
   if it dies. On a config error (exit 2) systemd leaves it stopped; launchd
   has no per-exit-code rule, so it retries once a minute and writes the
   same config error to the log each time until you fix the file.

6. **Prove the loop.** Send the assistant a message with intent `action` and
   time it. Expect the webhook call within about one poll interval of the
   mail arriving; the assistant's own run then takes however long it takes.

## Config reference

Top level:

| Key | Default | Meaning |
|---|---|---|
| `poll_seconds` | `12` | Seconds between polls of each signal. Minimum 5. |
| `state_dir` | `~/.local/state/tiding-signal-relay` | One small JSON file per target. |
| `targets` | — (required) | List of targets, below. |

Per target:

| Key | Default | Meaning |
|---|---|---|
| `name` | — (required) | Label used in logs and state. Letters, digits, `-`, `_`. |
| `signal_url` / `signal_url_file` | — (required) | The assistant's mail signal URL. |
| `webhook_url` / `webhook_url_file` | — (required) | The URL that starts a run. |
| `webhook_bearer` / `webhook_bearer_file` | none | Sent as `Authorization: Bearer …`. |
| `headers` | `{}` | Extra request headers, for platforms that want their own. |
| `payload` | the marker above | A fixed JSON object to send in its place. |
| `min_interval_seconds` | `60` | Minimum spacing between calls to this target. |
| `refire_after_seconds` | `0` (off) | Call again if the same mail is still unread after this long. `0`, or at least 300 and at least `min_interval_seconds`. |

`refire_after_seconds` covers a wake that started nothing (the run failed
before reading mail). It costs a run each time it fires, and it fires for as
long as the mail stays unread, so set it only for an assistant that reliably
acks what it handles.

The config path can also be set with `--config` or `TIDING_RELAY_CONFIG`.

## Security

- **Secrets:** the signal URL, the webhook URL and the bearer. All are read
  from owner-only files. None is logged: log lines carry the target name and
  a hostname, never a URL or header.
- **Redirects are refused.** A webhook that answers 3xx counts as a failed
  call, so the bearer is never sent to an address you did not configure.
- **https only**, except plain http to loopback for a relay on the same box
  as the server.
- **Blast radius:** a leaked signal URL reveals when mail arrives and how
  much is waiting, nothing else; revoke it with
  `DELETE /admin/signal/<address>`. A leaked webhook key lets someone start
  the assistant's runs; rotate it on the assistant's platform and replace
  the file.
- **Rotation:** replace the file and restart the relay. Secrets are read at
  start.
- **Proxies:** the relay honours the standard proxy environment variables.
  Do not set them for a relay that reaches its server over loopback http.
- **One slow target delays the others.** Targets are polled in turn, and a
  host that answers very slowly holds the turn. Keep unrelated fleets in
  separate relays.

## When something is wrong

| Log line or symptom | Meaning |
|---|---|
| `signal answers 404` | The signal was revoked or the URL is wrong. Issue a new one. |
| `signal poll failed (status 0 …)` | The server did not answer. The relay backs off and retries. |
| `webhook call FAILED (status 401/403 …)` | The webhook key is wrong or expired. |
| `webhook call FAILED (status 3xx …)` | The webhook redirected. Use its final URL. |
| Calls succeed, the assistant never runs | The platform accepted the call but started nothing. Test with `--fire-test` and read the platform's own run history. |
| A run on every poll | Not the relay: it calls once per cursor. Look for a second caller. |

`GET /admin/signal` on the server shows `last_polled_at` and `poll_count` per
signal. A relay that is running moves both.

## Tests

```bash
python3 -m pytest integrations/signal-relay/tests -q
```

They use loopback only. They cover the call/no-call decision, retry after a
failed call, burst coalescing, config and permission checks, refused
redirects (301 to 308, GET and POST), a non-HTTP answer, and that no log line
carries a secret.
