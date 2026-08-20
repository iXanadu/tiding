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
import os
import json
import shlex
import signal
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
    assert_local_seat,
    set_identity_anchor,
)

# Exit code for auth failure — distinct from 0 (clean) so a Monitor-armed
# session sees the watcher die with a reason instead of it silently
# retrying forever while every wake is missed.
EXIT_AUTH_FAILED = 2

# WATCH-1: the watcher refused to start because the identity it inherited
# contradicts the folder it was pointed at. Distinct code so a Monitor-armed
# session wakes on the refusal instead of silently listening at the wrong
# address for its whole life.
EXIT_IDENTITY_CONFLICT = 3
# Watch-claim (v2): DISPLACED means another live watcher holds this seat's
# watch — a supervisor should respawn-and-reclaim on a timer, never hot-loop.
# PARTIAL means our own listen set omits the seat we would claim for: a
# CONFIG error, not a race — retrying as-is can never succeed (F10).
EXIT_DISPLACED = 4
EXIT_PARTIAL_CLAIM = 5


def _identity_conflict(reader_identity: str, project: str,
                       explicit: bool) -> str | None:
    """WATCH-1: is this watcher about to listen somewhere it wasn't pointed?

    Measured four times (three on 2026-08-05, once on 2026-08-12): a watcher
    launched through a shell inherits identity state from an environment that
    predates it — a daemonized harness's frozen ``ENGRAM_INBOX_IDENTITY``, or
    a stale seat file reachable through a shared parent — and listens at that
    name while the session it serves is addressed at another. SILENT, because
    project-group mail still lands; the session is "correctly named and deaf."

    The rule: the winning identity must be the project's own name or a
    seat derived from it (``<project>`` / ``<project>-<anything>``). Anything
    else is cross-folder inheritance unless the operator ASSERTED it with
    ``--identity`` — stating an intent differs from leaking one, and
    co-working sessions depend on the explicit form.

    Returns the refusal message, or None when the identity is coherent.
    """
    if explicit:
        return None
    name = reader_identity.split("@", 1)[0]
    if name == project or name.startswith(f"{project}-"):
        return None
    return (
        f"inbox-wait: REFUSING to start — resolved identity {name!r} "
        f"contradicts --project-dir (which resolves to project {project!r}).\n"
        f"This watcher would listen at an address inherited from another "
        f"session's environment (a daemonized harness's frozen env, or a "
        f"stale seat file), and every DM to this session's real address "
        f"would silently miss.\n"
        f"THE FIX (almost always this one):\n"
        f"    relaunch with  --identity {project}\n"
        f"{name!r} is most likely LEAKED, not yours: unless YOU deliberately "
        f"chose that exact name for THIS session, do NOT assert it — "
        f"asserting a leaked name re-creates the silent misdelivery this "
        f"refusal exists to stop. (Deliberately chose it? Then, and only "
        f"then: --identity {name})"
    )

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


def _rearm_command() -> str:
    """The exact command that restarts THIS watcher with THIS configuration.

    Reconstructed from argv so the dying gasp never tells the session to
    re-arm with a guessed path or dropped flag — argv[0] is the absolute
    console-script path when launched per the startup doctrine, and every
    identity-bearing flag (--project-dir, --identity, --address) rides along
    verbatim.
    """
    return " ".join(shlex.quote(a) for a in sys.argv)


def _dying_gasp(reason: str) -> None:
    """WATCH-2 (owner order, 2026-08-17): never leave a live session deaf in
    silence. One structured STDOUT line on every exit path that ends coverage
    while the watched session is still alive — Monitor injects stdout lines
    as session-waking notifications, so this line IS the wake that tells the
    session to re-arm, and it carries the exact command so re-arming is one
    tool call, not an investigation. Sessions were observed deaf for 30+
    minutes because a watcher died (or never armed) with no last word; the
    roster's watcher_alive going stale is the server-side detector, but
    nothing was telling the SESSION.

    Deliberately NOT emitted when the watched session itself is gone (the
    farewell path) — there is nobody left to warn, and a gasp there would
    wake a successor with a stale instruction.
    """
    print(
        json.dumps(
            {
                "event": "watcher-dying",
                "reason": reason,
                "action": (
                    "ACTION REQUIRED: this session's inbox watcher is "
                    "exiting — mail will no longer wake you. Re-arm it NOW "
                    "under Monitor with the command below (fix the stated "
                    "reason first if it names one)."
                ),
                "command": _rearm_command(),
            },
            separators=(",", ":"),
        ),
        flush=True,
    )


def _emit_wake(w: dict) -> None:
    """One JSON line per wake — an utterance's ping, never a letter (O6).
    `ref` is where the record lives; nothing landed in the inbox."""
    print(
        json.dumps(
            {
                "event": "wake",
                "id": w.get("id"),
                "ref": w.get("ref"),
                "from": w.get("from_") or w.get("from"),
                "from_principal": w.get("from_principal"),
                "note": w.get("note", ""),
                "at": w.get("at"),
            },
            separators=(",", ":"),
        ),
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


# Intents counted as directives in the arm-time digest. fyi is deliberately
# absent (informational, never wakes — MSG-3). authority-directive is NOT
# here: it gets its own individual age-proof wake (LANE-3, owner override —
# the one intent whose queued presence must wake, however old).
_DIGEST_DIRECTIVE_INTENTS = {"action", "proceed", "escalate"}
_AUTHORITY_INTENT = "authority-directive"


def _is_immortal_address(to: str, occupant: str, project: str) -> bool:
    """Immortal = an address that outlives any one session: the project
    channel, a lane, a declared group, a #channel. Excluded: ``machine:``
    and the session's own occupant seat (seat-DM threads are seat-pinned and
    die with their occupant — settled in review; they never enter the
    digest). When the session is unseated its reader name IS the project —
    a channel — so the occupant exclusion applies only when they differ."""
    if not to:
        return False
    if to.startswith("machine:"):
        return False
    base = to.split("@", 1)[0]
    if base == occupant and occupant != project:
        return False
    return True


def _emit_estate_survey(entries: list, project: str) -> int:
    """Build-plan Step 6: the project-subtree's OPEN mail, surveyed at arm
    time — grouped by node, owner liveness split from register FACTS.

    The backlog digest above answers "what is waiting for ME"; this answers
    "what is waiting anywhere under this project" — the estate a successor
    inherits, including mail parked on dead incarnations' seats that no
    session's own listen_set covers. Counts and facts only, never bodies.

    Owner split, facts not verdicts (the register's honesty limits carry
    through): "live" = the allocator itself would skip the name for a live
    holder; "dead" = death evidence exists (a spawner's certificate, or a
    watcher-observed farewell — the register already voids farewells on
    later life); "none" = mail-only, no session was ever behind the name;
    anything else is "unknown" — quiet is not dead.

    Returns the number of nodes surveyed (0 = nothing emitted). Purely
    informational: never a wake, never affects one-shot exit.
    """
    nodes: dict = {}
    total = 0
    for e in entries:
        n = int(e.get("undrained_mail_count") or 0)
        if not n:
            continue
        # PRESENT LIFE BEATS HISTORICAL DEATH EVIDENCE. Found on this
        # feature's own first live verify (2026-08-18): a death cert keyed
        # on a REUSED session key (a launcher's slot key survives respawns)
        # attaches to the name's CURRENT holder, so death-first labeled a
        # live, currently-beating session "dead". A live-holder is beating
        # NOW; a cert is about some past process. Register-side residual
        # pinned as REG-DEATH-1.
        if (e.get("allocation") or {}).get("reason") == "live-holder":
            owner = "live"
        elif e.get("death"):
            owner = "dead"
        elif e.get("farewell_at"):
            owner = "dead"
        elif e.get("entry_type") == "mail-only":
            owner = "none"
        else:
            owner = "unknown"
        nodes[e.get("address")] = {
            "open_mail": n,
            "kind": e.get("entry_type"),
            "owner": owner,
        }
        total += n
    if not nodes:
        return 0
    print(
        json.dumps(
            {
                "event": "estate-survey",
                "note": (
                    "open mail across this project's subtree, by node — "
                    "the estate a successor inherits; owner from register "
                    "facts (quiet is not dead); read via memory_inbox, "
                    "drain per the wrapup rules"
                ),
                "project": project,
                "total_open": total,
                "nodes": nodes,
            },
            separators=(",", ":"),
        ),
        flush=True,
    )
    return len(nodes)


def _emit_backlog_digest(backlog: list, occupant: str, project: str) -> int:
    """LANE-3: ONE digest line for pre-arm backlog on immortal addresses,
    plus at most ONE individual wake for an unread authority-directive.

    Generalizes and REPLACES MSG-7's queued-directives line (one arm-time
    surface, never two): mail that arrives while no watcher is armed wakes
    nobody, and a successor's /startup sweep reads it as history — so the
    backlog on the addresses that outlive sessions (lane, project channel,
    groups, #channels) is surfaced as one summary, counts and senders only,
    never bodies. 105 queued messages are one line with count 105, not 105
    wakes — the wake path stays wake-on-new-only (created_at after ARM time,
    the seed boundary this watcher already implements).

    The unread state is the discriminator (per-reader read state, as today):
    mail a previous occupant actually read does not appear in the unread
    poll; mail nobody ever saw does. A mid-session re-arm re-digesting
    still-unread immortal mail is the same known behavior MSG-7 had.

    Exception, by design (owner override, existing intent, no new knob): an
    unread ``authority-directive`` gets ONE individual wake regardless of
    age — id + from + subject, no body; several collapse into one event
    carrying the newest plus a count.

    Returns the number of digest-worthy messages (0 = nothing emitted), so
    one-shot mode can treat a non-empty digest as its wake.
    """
    immortal = [
        m for m in backlog
        if _is_immortal_address(m.get("to") or "", occupant, project)
    ]
    authority = [
        m for m in backlog
        if (m.get("intent") or "").strip().lower() == _AUTHORITY_INTENT
    ]
    emitted = 0
    if immortal:
        per_addr: dict = {}
        for m in immortal:
            a = per_addr.setdefault(
                m.get("to"),
                {"unread_count": 0, "oldest_at": None, "newest_at": None,
                 "senders": {}},
            )
            a["unread_count"] += 1
            c = m.get("created_at")
            if c:
                a["oldest_at"] = min(filter(None, [a["oldest_at"], c]))
                a["newest_at"] = max(filter(None, [a["newest_at"], c]))
            frm = m.get("from") or m.get("from_") or "?"
            a["senders"][frm] = a["senders"].get(frm, 0) + 1
        directive_count = sum(
            1 for m in immortal
            if (m.get("intent") or "").strip().lower()
            in _DIGEST_DIRECTIVE_INTENTS
        )
        print(
            json.dumps(
                {
                    "event": "backlog-digest",
                    "note": (
                        "unread pre-arm mail on this session's immortal "
                        "addresses (lane/project/groups/channels) — arrived "
                        "while no watcher was armed; counts only, read via "
                        "memory_inbox"
                    ),
                    "addresses": {
                        to: {
                            "unread_count": a["unread_count"],
                            "oldest_at": a["oldest_at"],
                            "newest_at": a["newest_at"],
                            "top_senders": sorted(
                                a["senders"], key=a["senders"].get,
                                reverse=True,
                            )[:3],
                        }
                        for to, a in per_addr.items()
                    },
                    "directive_count": directive_count,
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
        emitted = len(immortal)
    if authority:
        newest = max(authority, key=lambda m: m.get("created_at") or "")
        print(
            json.dumps(
                {
                    "event": "authority-directive-queued",
                    "note": (
                        "unread authority-directive predating this watcher — "
                        "age does not waive this intent; handle via "
                        "memory_inbox"
                    ),
                    "count": len(authority),
                    "id": newest.get("id"),
                    "from": newest.get("from") or newest.get("from_"),
                    "subject": newest.get("subject", ""),
                    "created_at": newest.get("created_at"),
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
        emitted += len(authority)
    return emitted


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


class _WatchClaimState:
    """The watcher's side of one-seat-one-watch (docs/design/watch-claim.md v2).

    The contract this implements, in the reviewer's words: refused launchers
    RE-CLAIM ON A TIMER (exit-forever meant mail died with whichever process
    claimed first); a beat whose response is LOST means holder-unknown — stop
    emitting until a verdict, because emitting while unsure is how two
    watchers deliver the same mail twice; and when the claim API itself is
    unreachable, the watcher RUNS UNCLAIMED and loudly UNHELD — the repair
    crew hears each other while the store is sick (K3), the register just
    never shows the seat as covered.
    """

    def __init__(self, seat: str):
        import secrets
        self.seat = seat
        self.nonce = secrets.token_hex(16)   # random, never pid (ghost class)
        self.held = False
        self.unheld_mode = False             # K3: running without a claim
        # K2 delivery-liveness: the newest mail created_at this process has
        # actually EMITTED. Beating proves existence; THIS proves delivery,
        # and a holder that beats without advancing it while mail waits is
        # displaceable — the beating-but-mute monopoly cannot form.
        self.fetched_through: str | None = None

    async def acquire(self, client, project_dir: str, listen_set: list[str]) -> int | None:
        """Claim, retrying on `held`. Returns an EXIT_ code only for the one
        unretryable outcome (partial); None once we hold OR run unheld."""
        import asyncio as _a
        while True:
            try:
                r = await client.watch_claim(
                    seat=self.seat, nonce=self.nonce, armed_by="bridge",
                    project_dir=project_dir or "", listen_set=listen_set,
                )
            except Exception as e:
                self.unheld_mode = True
                print(
                    "inbox-wait: ⚠ UNHELD — watch-claim API unreachable "
                    f"({e.__class__.__name__}); running WITHOUT a claim. Mail "
                    "still wakes this session, but the register will not show "
                    "the seat as covered.", file=sys.stderr, flush=True)
                return None
            v = r.get("verdict")
            if v == "granted":
                self.held = True
                return None
            if v == "partial-refused":
                print(f"inbox-wait: watch claim PARTIAL-REFUSED: {r.get('reason')}",
                      file=sys.stderr, flush=True)
                return EXIT_PARTIAL_CLAIM
            retry = float(r.get("retry_after_seconds") or 150)
            print(f"inbox-wait: watch held by {r.get('holder_armed_by')!r} — "
                  f"re-claiming in {retry:.0f}s", file=sys.stderr, flush=True)
            await _a.sleep(retry)

    async def beat(self, client) -> str:
        """One watch beat: 'holder' | 'displaced' | 'unknown'.

        Three outcomes, three behaviors — conflating any two recreates a
        tonight-bug: displaced -> EXIT (a successor holds; emitting doubles
        delivery); unknown (lost response) -> PAUSE emission this cycle only
        (a server blip must not kill a long-lived watcher, but emitting
        while holder-unknown is how two watchers deliver the same mail
        twice); holder -> proceed.
        """
        if self.unheld_mode:
            return "holder"  # no claim to defend; emission legitimate (K3)
        try:
            r = await client.watch_beat(self.seat, self.nonce,
                                        fetched_through=self.fetched_through)
        except Exception:
            print("inbox-wait: watch beat lost — pausing emission until a "
                  "verdict", file=sys.stderr, flush=True)
            return "unknown"
        if r.get("verdict") == "holder":
            return "holder"
        print("inbox-wait: DISPLACED — another watcher holds this seat; "
              "exiting for supervisor respawn", file=sys.stderr, flush=True)
        return "displaced"


def _open_fifo_for_write(path: str):
    """Open the wake FIFO, BLOCKING until a reader attaches.

    Load-bearing ordering (review rider): the claim is not taken until the
    tail is attached, because this open() cannot return before a reader
    exists. A FIFO nobody tails therefore never claims — it cannot become
    F4-with-extra-steps. And if the reader later detaches, writes block, the
    poll loop stalls, beats stop, and the claim EXPIRES — coverage honestly
    releases itself with no code asked to notice.
    """
    import os as _os
    if not _os.path.exists(path):
        _os.mkfifo(path, 0o600)
    print(f"inbox-wait: waiting for a wake consumer on {path} "
          "(claim follows attach)", file=sys.stderr, flush=True)
    f = open(path, "w", buffering=1)
    print("inbox-wait: consumer attached", file=sys.stderr, flush=True)
    return f


async def _run(args) -> int:
    # This process's cwd is whatever shell launched it, not the session's
    # project root, so `--project-dir` is the only authoritative anchor the
    # watcher has. Declare it once, before any identity is computed — the
    # bridge needs no equivalent, its spawn cwd already is the anchor.
    set_identity_anchor(args.project_dir or None)
    # --identity is the operator's explicit assertion (WATCH-1). It takes the
    # runtime-seat slot — the highest precedence resolve_session_identity
    # knows — so it beats a poisoned env AND a stale seat file, the two
    # inheritance vectors this flag exists to override.
    explicit_identity = bool(getattr(args, "identity", "") or "")
    if explicit_identity:
        assert_local_seat(args.identity)
    reader_identity, listen_set = compute_identity(args.project_dir or None)

    # WATCH-1: refuse a contradictory inherited identity at arm time, loudly,
    # rather than listening at the wrong address for the session's whole life.
    conflict = _identity_conflict(
        reader_identity,
        derive_project_name(args.project_dir or None),
        explicit_identity,
    )
    if conflict:
        print(conflict, file=sys.stderr, flush=True)
        return EXIT_IDENTITY_CONFLICT

    if args.address:
        listen_set = [a.strip() for a in args.address.split(",") if a.strip()]

    _warn_plaintext_url(settings.memory_api_url)
    client = MemoryClient(settings.memory_api_url, settings.memory_api_token)

    # v2 spawn path (watch-claim design). ORDER IS LOAD-BEARING: the FIFO
    # attach comes FIRST, because open-for-write blocks until a reader
    # exists — so with --claim, coverage is never claimed for a wake stream
    # nobody consumes (the F4-with-extra-steps case from review).
    if getattr(args, "fifo", ""):
        fifo_f = _open_fifo_for_write(args.fifo)
        os.dup2(fifo_f.fileno(), 1)  # all emit prints now land in the FIFO;
        #                              stderr stays a real log (never DEVNULL —
        #                              AB's maintenance watcher died silently
        #                              behind exactly that)
    claim_state = None
    if getattr(args, "claim", False):
        claim_state = _WatchClaimState(reader_identity.split("@", 1)[0])
        code = await claim_state.acquire(
            client, args.project_dir or "", listen_set)
        if code is not None:
            return code

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
        # LANE-3 exception (subsumes MSG-7's queued-directives line — one
        # arm-time surface, never two): pre-arm backlog on IMMORTAL addresses
        # gets ONE digest line (counts + senders, no bodies), and an unread
        # authority-directive gets one individual wake regardless of age.
        # Queued-while-down and delivered-live are different outcomes, not
        # degrees of one — mail sent into a restart window otherwise becomes
        # history nobody acts on, with no error on either side.
        # See _emit_backlog_digest.
        seen: set = set()
        if not args.include_existing:
            try:
                backlog = await _poll(client, listen_set, reader_identity, seen)
                occupant = reader_to_address(reader_identity or "")
                anchor_project = derive_project_name(args.project_dir or None)
                digest_hits = _emit_backlog_digest(
                    backlog, occupant, anchor_project)
                # Step 6: the subtree estate, own try — informational, never
                # a wake, and a register hiccup must not cost the digest or
                # the arm (an old server without ADDR-REG 404s here).
                try:
                    reg = await client.session_addresses(
                        project=anchor_project)
                    _emit_estate_survey(
                        reg.get("entries") or [], anchor_project)
                except Exception as se:
                    print(f"inbox-wait: estate survey skipped ({se})",
                          file=sys.stderr, flush=True)
                if digest_hits and not args.follow:
                    return 0  # one-shot: the digest IS the wake
            except Exception as e:  # seeding failure is non-fatal — start clean
                if _auth_error_code(e):
                    print(_AUTH_FAIL_MSG.format(code=_auth_error_code(e)), file=sys.stderr, flush=True)
                    _dying_gasp(
                        f"server rejected credentials "
                        f"({_auth_error_code(e)}) — fix the token in "
                        f"~/.config/engram/identity, then re-arm"
                    )
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
                    # WATCH-1, mid-run edition: a re-resolved identity that
                    # contradicts the project is NOT followed. At arm time we
                    # refuse and exit; here the session is live and dying
                    # would deafen it completely, so the watcher HOLDS its
                    # coherent identity and says so each time. A legitimate
                    # runtime re-seat is project-derived and follows normally.
                    mid_conflict = _identity_conflict(
                        new_identity,
                        derive_project_name(args.project_dir or None),
                        explicit_identity,
                    )
                    if mid_conflict:
                        print(
                            f"inbox-wait: IGNORING seat change "
                            f"{reader_identity!r} -> {new_identity!r} — the "
                            f"new identity contradicts --project-dir; holding "
                            f"{reader_identity!r}. If the change is "
                            f"deliberate, re-arm with --identity.",
                            file=sys.stderr, flush=True,
                        )
                    else:
                        print(
                            f"inbox-wait: seat changed {reader_identity!r} -> "
                            f"{new_identity!r}; now listening on {new_listen}",
                            file=sys.stderr, flush=True,
                        )
                        reader_identity, listen_set = new_identity, new_listen
            if claim_state is not None:
                wv = await claim_state.beat(client)
                if wv == "displaced":
                    return EXIT_DISPLACED
                if wv == "unknown":
                    await asyncio.sleep(args.poll_interval)
                    continue
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
                    _dying_gasp(
                        f"server rejected credentials "
                        f"({_auth_error_code(e)}) — fix the token in "
                        f"~/.config/engram/identity, then re-arm"
                    )
                    return EXIT_AUTH_FAILED
                # transient server blip (e.g. macmini restart) must not kill a
                # long-lived --follow watcher; log and keep polling.
                print(f"inbox-wait: poll error ({e}); retrying", file=sys.stderr, flush=True)
                fresh = []

            for m in fresh:
                _emit(m)
                if claim_state is not None and m.get("created_at"):
                    ft = claim_state.fetched_through
                    if ft is None or m["created_at"] > ft:
                        claim_state.fetched_through = m["created_at"]
            # Band D 10a: ephemeral wakes ride the same poll timer. Own try —
            # a pre-10a server has no endpoint (404) and that must cost this
            # watcher nothing. The server never pops (shared listen_sets have
            # several live waiters); `seen` dedupes by wake id like mail.
            wakes = []
            try:
                wresp = await client.wake_poll(
                    listen_set=listen_set, reader_identity=reader_identity)
                for w in (wresp.get("wakes") or []):
                    wid = w.get("id")
                    if not wid or wid in seen:
                        continue
                    seen.add(wid)
                    _emit_wake(w)
                    wakes.append(w)
            except Exception as we:
                # An AUTH refusal is not additive-degradation: a watcher whose
                # wake socket is refused on every poll silently misses every
                # room ping while looking covered — the same fail-open the
                # mail path already dies loudly on. Anything else (404 from a
                # pre-10a server, a transient blip) costs nothing.
                if _auth_error_code(we):
                    print(_AUTH_FAIL_MSG.format(code=_auth_error_code(we)),
                          file=sys.stderr, flush=True)
                    _dying_gasp(
                        f"server rejected credentials on the wake socket "
                        f"({_auth_error_code(we)}) — fix the token in "
                        f"~/.config/engram/identity, then re-arm"
                    )
                    return EXIT_AUTH_FAILED
            if (fresh or wakes) and not args.follow:
                return 0  # one-shot: exit on first new mail OR wake
            if deadline is not None and time.monotonic() >= deadline:
                # WATCH-2: an expired deadline ends coverage while the session
                # lives — that must never be silent (it was: bare `return 0`).
                _dying_gasp(
                    f"--timeout {args.timeout:.0f}s reached with the watched "
                    f"session still alive"
                )
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
                    # No gasp here, deliberately: the SESSION is what died,
                    # so there is nobody to tell to re-arm (WATCH-2).
                    await _farewell(client, reader_identity, args.project_dir or None)
                    return 0
            else:
                gone_seen = 0
            await asyncio.sleep(args.poll_interval)
    except (KeyboardInterrupt, SystemExit):
        raise  # signal paths gasp in main(), where the reason is known
    except Exception as e:
        # WATCH-2: an unexpected crash is the least excusable silent death —
        # say so and hand over the re-arm command before propagating.
        _dying_gasp(f"unexpected watcher crash: {e!r}")
        raise
    finally:
        await client.close()


def main() -> None:
    p = argparse.ArgumentParser(
        prog="engram-inbox-wait",
        description="Watch the engram inbox and emit on new mail (wakes a dormant session).",
    )
    p.add_argument("--project-dir", default="", help="working dir for identity (project/listen_set resolution)")
    p.add_argument("--address", default="", help="CSV override of the listen_set to watch")
    p.add_argument("--identity", default="", help=(
        "explicit identity assertion (WATCH-1): listen as this seat, "
        "overriding any inherited env or seat file. Required when the "
        "intended identity does not derive from the project name."
    ))
    p.add_argument("--poll-interval", type=float, default=45.0, help="seconds between polls (default 45)")
    p.add_argument("--follow", action="store_true", help="keep running, emit a line per message (Monitor mode)")
    p.add_argument("--timeout", type=float, default=0.0, help="max seconds to wait in one-shot mode (0=forever)")
    p.add_argument("--include-existing", action="store_true", help="also wake on already-unread backlog")
    p.add_argument("--claim", action="store_true", help=(
        "hold the one-seat-one-watch claim (watch-claim v2): claim before "
        "polling, beat each poll, exit DISPLACED for supervisor respawn; "
        "runs UNHELD (loud) if the claim API is unreachable"))
    p.add_argument("--fifo", default="", help=(
        "write wake lines to this FIFO instead of stdout; open BLOCKS until "
        "a consumer attaches, and with --claim the claim follows the attach "
        "— a FIFO nobody tails never claims coverage"))
    args = p.parse_args()
    if args.timeout <= 0:
        args.timeout = None

    # WATCH-2: a kill must not be a silent deafening. SIGTERM is how a
    # supervisor or TaskStop ends this process; the handler raises SystemExit
    # so the asyncio loop unwinds, and the gasp below tells the session it is
    # now deaf and exactly how to re-arm. SIGKILL cannot be caught — for that
    # death the roster's watcher_alive going stale is the only detector.
    def _on_sigterm(signum, frame):
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, _on_sigterm)

    try:
        sys.exit(asyncio.run(_run(args)))
    except KeyboardInterrupt:
        _dying_gasp("interrupted (SIGINT) with the session possibly alive")
        sys.exit(0)
    except SystemExit as e:
        if e.code == 143:
            _dying_gasp("killed (SIGTERM) with the session possibly alive")
        raise


if __name__ == "__main__":
    main()
