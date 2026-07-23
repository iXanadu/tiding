"""engram-inbox-wait — shell-callable inbox watcher.

Closes the "a sender can't see its own reply" gap: a Claude session is dormant
between turns and only resumes when the human types, so it never learns a reply
arrived without a human relaying it. This watcher is a plain shell process that
polls the inbox and emits on new mail, which the Claude Code harness turns into
a wake-up two ways:

  * Bash run_in_background — the command EXITS when there's new mail, and the
    harness re-invokes the session on exit. One wake. Use the default (one-shot).
  * Monitor tool — each stdout line becomes an injected notification. Use
    ``--follow`` to keep running and print one JSON line per new message, so a
    session armed at /startup listens continuously ("always on").

Auth + identity reuse the MCP bridge exactly (engram_mcp.config.settings +
compute_identity) — no second auth path to drift. IMPORTANT: a bare shell
invocation does NOT inherit the bridge's ~/.claude.json env, so the token must
live in ~/.config/engram/identity (the durable identity file) for the watcher
to authenticate.

The watcher NEVER acks — it only wakes the session; the session reads and handles
each message itself. (Acking here would hide mail from the very session that must
act on it.)
"""

import argparse
import asyncio
import json
import sys
import time
from urllib.parse import urlparse

import httpx

from engram_mcp.client import MemoryClient
from engram_mcp.config import settings
from engram_mcp.identity import compute_identity, reader_to_address

# Exit code for auth failure — distinct from 0 (clean) so a Monitor-armed
# session sees the watcher die with a reason instead of it silently
# retrying forever while every wake is missed.
EXIT_AUTH_FAILED = 2

_AUTH_FAIL_MSG = (
    "inbox-wait: FATAL — server rejected this watcher's credentials ({code}). "
    "Exiting rather than silently missing every wake. Fix the token in "
    "~/.config/engram/identity (the watcher does NOT inherit the MCP bridge "
    "env), then re-arm the watcher."
)


def _auth_error_code(e: Exception) -> int | None:
    """Return 401/403 when the exception is an auth rejection, else None."""
    if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (401, 403):
        return e.response.status_code
    return None


def _warn_plaintext_url(url: str) -> None:
    """Warn when the token would travel plaintext beyond this box."""
    parsed = urlparse(url)
    if parsed.scheme == "http" and parsed.hostname not in (
        "localhost", "127.0.0.1", "::1",
    ):
        print(
            f"inbox-wait: WARNING — {url} is plain http to a non-local host; "
            "the auth token travels unencrypted. Prefer https or a private "
            "overlay network (e.g. Tailscale).",
            file=sys.stderr,
            flush=True,
        )


def _emit(msg: dict) -> None:
    """Print one compact JSON line per new message (Monitor → one wake each)."""
    print(
        json.dumps(
            {
                "id": msg.get("id"),
                "from": msg.get("from") or msg.get("from_"),
                "subject": msg.get("subject", ""),
                "thread_id": msg.get("thread_id"),
                "created_at": msg.get("created_at"),
            },
            separators=(",", ":"),
        ),
        flush=True,
    )


def _own_addresses(reader_identity: str | None) -> set:
    """The set of addresses that identify THIS watcher's own session.

    A session sends with ``from_ = reader_identity`` (full ``<name>@<host>``
    form), but its loose role name is also its own. Either, seen as a message
    ``from``, means the mail is our own outbound echoed back — never a wake.
    """
    own = set()
    if reader_identity:
        own.add(reader_identity.strip().lower())
        own.add(reader_to_address(reader_identity).strip().lower())
    return {a for a in own if a}


async def _poll(client, listen_set, reader_identity, seen: set) -> list:
    """Return new (unseen) messages, recording their ids in ``seen``.

    Self-echo guard: a session listens on the same loose name it SENDS to (e.g.
    ``beastchat`` is in both the listen_set and a ``to:`` target), and the inbox
    has no from==self filter, so a session's own outbound comes back as inbound
    and wakes it. We drop any message whose ``from`` is one of our own addresses
    so the watcher never wakes on mail this session itself sent. (When two
    distinct sessions share one identity, split their inbox addresses so this
    stays precise — see decision/three-axes-principal-project-address.)
    """
    # newest_first is load-bearing: the watcher never acks, so its unread set
    # only grows. With the default oldest-first order, once unread exceeds the
    # limit the newest mail is truncated out of the window and never emitted —
    # the watcher goes blind exactly when the inbox is busy. Newest-first keeps
    # new arrivals in the window regardless of backlog size; `seen` dedups.
    result = await client.inbox_list(
        listen_set=listen_set,
        reader_identity=reader_identity,
        unread_only=True,
        limit=50,
        newest_first=True,
    )
    if result.get("status") != "ok":
        raise RuntimeError(f"inbox status={result.get('status')!r}")
    own = _own_addresses(reader_identity)
    fresh = []
    for m in result.get("messages", []):
        mid = m.get("id")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        sender = (m.get("from") or m.get("from_") or "").strip().lower()
        if sender and sender in own:
            # our own outbound, echoed back — record as seen but never wake on it
            print(f"inbox-wait: skip self-echo {mid} (from {sender!r})", file=sys.stderr, flush=True)
            continue
        if (m.get("intent") or "").strip().lower() == "fyi":
            # MSG-3 wake-gating: fyi is informational — record as seen so it
            # never wakes, but leave it unacked for the next interactive read.
            print(f"inbox-wait: skip fyi {mid} (no wake)", file=sys.stderr, flush=True)
            continue
        fresh.append(m)
    return fresh


async def _run(args) -> int:
    reader_identity, listen_set = compute_identity(args.project_dir or None)
    if args.address:
        listen_set = [a.strip() for a in args.address.split(",") if a.strip()]

    _warn_plaintext_url(settings.memory_api_url)
    client = MemoryClient(settings.memory_api_url, settings.memory_api_token)
    try:
        # Default: only wake on mail that arrives AFTER the watcher starts — a
        # startup watcher shouldn't immediately fire on the backlog the session
        # already saw at /startup. --include-existing opts into the backlog too.
        seen: set = set()
        if not args.include_existing:
            try:
                await _poll(client, listen_set, reader_identity, seen)
            except Exception as e:  # seeding failure is non-fatal — start clean
                if _auth_error_code(e):
                    print(_AUTH_FAIL_MSG.format(code=_auth_error_code(e)), file=sys.stderr, flush=True)
                    return EXIT_AUTH_FAILED
                print(f"inbox-wait: seed poll failed ({e})", file=sys.stderr, flush=True)

        deadline = (time.monotonic() + args.timeout) if args.timeout else None
        while True:
            # SEAT-2: re-resolve identity every poll, so a seat taken mid-session
            # by our sibling bridge reaches us WITHOUT a restart.
            #
            # Before this, a runtime re-seat left the session addressed at the
            # new seat while this watcher still listened at the old one — and
            # because project-addressed mail kept arriving, the failure was
            # quiet rather than obvious. An explicit "now re-arm your watcher"
            # instruction is discipline; this is inheritance.
            #
            # --address is an explicit operator override and always wins.
            if not args.address:
                new_identity, new_listen = compute_identity(args.project_dir or None)
                if new_identity != reader_identity:
                    print(
                        f"inbox-wait: seat changed {reader_identity!r} -> "
                        f"{new_identity!r}; now listening on {new_listen}",
                        file=sys.stderr, flush=True,
                    )
                    reader_identity, listen_set = new_identity, new_listen
            try:
                fresh = await _poll(client, listen_set, reader_identity, seen)
            except Exception as e:
                # AUTH rejection is not transient: retry-forever here means the
                # server refuses this watcher on every poll while the session
                # believes it's covered — every wake silently missed (fail-open,
                # 2026-07-21 audit). Die loudly; the Monitor exit wakes the
                # session with the reason.
                if _auth_error_code(e):
                    print(_AUTH_FAIL_MSG.format(code=_auth_error_code(e)), file=sys.stderr, flush=True)
                    return EXIT_AUTH_FAILED
                # transient server blip (e.g. macmini restart) must not kill a
                # long-lived --follow watcher; log and keep polling.
                print(f"inbox-wait: poll error ({e}); retrying", file=sys.stderr, flush=True)
                fresh = []

            for m in fresh:
                _emit(m)
            if fresh and not args.follow:
                return 0  # one-shot: exit on first new mail (Bash-bg = one wake)
            if deadline is not None and time.monotonic() >= deadline:
                return 0
            await asyncio.sleep(args.poll_interval)
    finally:
        await client.close()


def main() -> None:
    p = argparse.ArgumentParser(
        prog="engram-inbox-wait",
        description="Watch the engram inbox and emit on new mail (wakes a dormant session).",
    )
    p.add_argument("--project-dir", default="", help="working dir for identity (project/listen_set resolution)")
    p.add_argument("--address", default="", help="CSV override of the listen_set to watch")
    p.add_argument("--poll-interval", type=float, default=45.0, help="seconds between polls (default 45)")
    p.add_argument("--follow", action="store_true", help="keep running, emit a line per message (Monitor mode)")
    p.add_argument("--timeout", type=float, default=0.0, help="max seconds to wait in one-shot mode (0=forever)")
    p.add_argument("--include-existing", action="store_true", help="also wake on already-unread backlog")
    args = p.parse_args()
    if args.timeout <= 0:
        args.timeout = None
    try:
        sys.exit(asyncio.run(_run(args)))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
