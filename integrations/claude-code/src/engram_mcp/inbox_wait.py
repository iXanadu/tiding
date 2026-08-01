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
from engram_mcp.identity import (
    compute_identity,
    derive_project_name,
    discover_session_process,
    process_is_gone,
    reader_to_address,
)

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


# Intents whose queued presence at watcher start is worth one summary wake.
# fyi is deliberately absent (informational, never wakes — MSG-3); an omitted
# intent is legacy default and DOES wake live, but seeding past legacy mail is
# the pre-MSG-7 status quo and most of it is chatter — the summary is for mail
# that states its own urgency.
_DIRECTIVE_INTENTS = {"action", "proceed", "escalate", "authority-directive"}


def _emit_queued_directives(backlog: list) -> None:
    """MSG-7: surface directives that were queued while no watcher was armed.

    Returns the number of queued directives (0 when the line was not emitted),
    so one-shot mode can treat a non-empty summary as its wake.

    Mail that arrives during a restart window is delivered but wakes nobody —
    and the next session's /startup sweep reads it as HISTORY, so a directive
    sent to the predecessor becomes context instead of an instruction. The
    sender saw a successful send; the reader saw background. Neither errored.

    The seed still swallows the backlog (a startup watcher must not firehose
    the mail the session already saw at /startup) — but unacked directive-
    intent mail gets ONE summary line. The ack is the discriminator, and it is
    the right one: mail the previous session actually HANDLED is acked and
    does not appear here; mail it merely read past is still open and does.
    One line, not one per message: a week-old unacked backlog must not turn
    watcher-arm into thirty wakes.
    """
    queued = [
        m for m in backlog
        if (m.get("intent") or "").strip().lower() in _DIRECTIVE_INTENTS
    ]
    if not queued:
        return 0
    print(
        json.dumps(
            {
                "event": "queued-directives",
                "note": (
                    "these arrived while no watcher was armed (e.g. during a "
                    "restart) and woke nobody — read them as DIRECTIVES, not "
                    "history; handle via memory_inbox"
                ),
                "count": len(queued),
                "messages": [
                    {
                        "id": m.get("id"),
                        "from": m.get("from") or m.get("from_"),
                        "subject": m.get("subject", ""),
                        "intent": m.get("intent"),
                        "created_at": m.get("created_at"),
                    }
                    for m in queued[:10]
                ],
            },
            separators=(",", ":"),
        ),
        flush=True,
    )
    return len(queued)


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


async def _beat(client, reader_identity: str, project_dir: str | None) -> None:
    """Tell the server an EAR is alive at this address (MSG-5, SEAT-7).

    Best-effort and deliberately silent on failure: a session's ability to
    wake must never depend on the bookkeeping that reports it can.

    Why the watcher and not the bridge: the bridge heartbeat rides tool calls,
    so it measures ACTIVITY — a session heads-down on a long build stops
    beating and ages toward reclaimable while it is alive and listening. This
    process polls on its own timer and lives exactly as long as the session,
    so it measures EXISTENCE. It is also the only process whose presence means
    inbound mail actually reaches somebody.
    """
    try:
        await client.presence_update(
            identity=reader_identity.split("@", 1)[0],
            project=derive_project_name(project_dir),
            project_dir=project_dir,
            watcher=True,
        )
    except Exception:
        pass


async def _farewell(client, reader_identity: str, project_dir: str | None) -> None:
    """Report the watched session gone. Best-effort, like every other beat."""
    try:
        await client.presence_farewell(
            identity=reader_identity.split("@", 1)[0],
            project=derive_project_name(project_dir),
            project_dir=project_dir,
        )
    except Exception:
        pass


async def _run(args) -> int:
    reader_identity, listen_set = compute_identity(args.project_dir or None)
    if args.address:
        listen_set = [a.strip() for a in args.address.split(",") if a.strip()]

    _warn_plaintext_url(settings.memory_api_url)
    client = MemoryClient(settings.memory_api_url, settings.memory_api_token)

    # Resolved ONCE, at arm time, while the session is definitely alive — a
    # later lookup could resolve a recycled pid or find nothing and read that
    # as a death. None means "no session identified", which stays permanently
    # distinct from "the session is gone": we simply never report a farewell.
    watched = discover_session_process()
    if watched is None:
        print(
            "inbox-wait: no session process identified — wake still works, "
            "but this watcher will not report the session's exit",
            file=sys.stderr, flush=True,
        )
    gone_seen = 0  # consecutive polls that positively observed the exit

    try:
        # Default: only wake on mail that arrives AFTER the watcher starts — a
        # startup watcher shouldn't immediately fire on the backlog the session
        # already saw at /startup. --include-existing opts into the backlog too.
        #
        # MSG-7 exception: unacked DIRECTIVE-intent mail in that backlog gets
        # one summary line. Queued-while-down and delivered-live are different
        # outcomes, not degrees of one — a directive sent into a restart window
        # otherwise becomes history nobody acts on, with no error on either
        # side. See _emit_queued_directives.
        seen: set = set()
        if not args.include_existing:
            try:
                backlog = await _poll(client, listen_set, reader_identity, seen)
                if _emit_queued_directives(backlog) and not args.follow:
                    return 0  # one-shot: queued directives ARE the wake
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
            await _beat(client, reader_identity, args.project_dir or None)
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

            # OBSERVE the session, don't announce our own death. We outlive it
            # — measured: the watcher sits in its own process group, so a
            # group-directed kill aimed at the session never reaches us and an
            # exit hook would simply not fire on the commonest death. Being
            # alive is what makes this reportable at all.
            if watched and process_is_gone(*watched):
                # Confirm on a second poll before reporting. `process_is_gone`
                # already refuses to answer when it could not ask, so this is
                # belt-and-braces — but a false farewell is the expensive
                # direction, and one extra poll interval of latency on a death
                # report costs nothing that matters.
                gone_seen += 1
                if gone_seen >= 2:
                    await _farewell(client, reader_identity, args.project_dir or None)
                    return 0
            else:
                gone_seen = 0
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
