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

from engram_mcp.client import MemoryClient
from engram_mcp.config import settings
from engram_mcp.identity import compute_identity


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


async def _poll(client, listen_set, reader_identity, seen: set) -> list:
    """Return new (unseen) messages, recording their ids in ``seen``."""
    result = await client.inbox_list(
        listen_set=listen_set,
        reader_identity=reader_identity,
        unread_only=True,
        limit=50,
    )
    if result.get("status") != "ok":
        raise RuntimeError(f"inbox status={result.get('status')!r}")
    fresh = []
    for m in result.get("messages", []):
        mid = m.get("id")
        if mid and mid not in seen:
            seen.add(mid)
            fresh.append(m)
    return fresh


async def _run(args) -> int:
    reader_identity, listen_set = compute_identity(args.project_dir or None)
    if args.address:
        listen_set = [a.strip() for a in args.address.split(",") if a.strip()]

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
                print(f"inbox-wait: seed poll failed ({e})", file=sys.stderr, flush=True)

        deadline = (time.monotonic() + args.timeout) if args.timeout else None
        while True:
            try:
                fresh = await _poll(client, listen_set, reader_identity, seen)
            except Exception as e:
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
