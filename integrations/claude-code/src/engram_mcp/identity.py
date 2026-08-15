"""Compute the local session's inbox identity and listen_set.

An inbox "address" is a flat string. A Claude session listens on a **set** of
addresses — typically its project and its machine — and is addressed by the
same tuple when ack'ing read receipts.

The rule:
    1. Walk up from CWD looking for ``.engram.cfg`` — if found, use declared name
    2. Else CWD has a ``/projects/<name>/`` segment → project session, name = <name>
    3. Else (``~``, ``/opt/srv``, ``/tmp``, bare ``~/projects``) → admin

Project session:
    reader_identity = "<name>@<host>"
    listen_set      = ["<name>", "machine:<host>", "<name>@<host>"]

Admin session:
    reader_identity = "admin@<host>"
    listen_set      = ["admin", "machine:<host>", "admin@<host>"]

``project_dir`` comes from the tool call (passed by Claude Code via the
``project_dir`` parameter on engram_mcp tools) — NOT from ``os.getcwd()``
inside this subprocess, which is unreliable (see commit 223b17b).
"""

import os
import socket
import subprocess

from engram_mcp.scoping import (
    resolve_inbox_groups,
    resolve_inbox_identity,
    resolve_project_name,
)

ADMIN_NAME = "admin"

# LANE-2: projects that never grow provider lanes. `admin` is ONE role worn
# by maintenance sessions fleet-wide on purpose (SEAT-ADMIN-1) — an
# `admin-<provider>` lane would detach sessions from the role again.
ADMIN_EXEMPT_LANE_PROJECTS = {ADMIN_NAME}

# Opt-in per-session inbox identity. When set, this session is ADDRESSED as
# ``<value>@<host>`` and sends FROM that identity, while still joining its
# project's group address for broadcasts. MEMORY scoping is unaffected — it
# derives from .engram.cfg, not this. This is how two sessions that share one
# project (and thus shared scope=project memory) get DISTINCT inbox identities
# so they can DM each other and so the watcher's self-echo filter stays precise.
#
# Two sources, in precedence order:
#   1. ENGRAM_INBOX_IDENTITY env var (override / escape hatch)
#   2. ``inbox_identity = <name>`` in .engram.cfg (the durable, per-repo,
#      version-controlled source — preferred, since the claude-memory MCP
#      server is registered ONCE globally with no per-session env block, so
#      .engram.cfg resolved from project_dir is the only per-session knob)
# See decision/three-axes-principal-project-address.
INBOX_IDENTITY_ENV = "ENGRAM_INBOX_IDENTITY"

# Set when the launch env overrides a DIFFERENT identity declared in
# .engram.cfg. Rendered once as a banner by the bridge (see
# _identity_override_banner) so a dead declared address cannot sit unnoticed.
_IDENTITY_OVERRIDE_NOTICE: str | None = None


def identity_override_notice() -> str | None:
    """The pending 'your .engram.cfg declaration is not in effect' notice."""
    return _IDENTITY_OVERRIDE_NOTICE



# Session-stable project_dir resolution.
#
# Inbox identity is derived from the per-call ``project_dir`` argument. The
# caller (the LLM) is expected to pass it on EVERY tool call, but nothing forces
# that — and when a call omits it, ``derive_project_name(None)`` falls back to
# ``admin``. A session that passed project_dir on reads (``memory_inbox``) but
# omitted it on writes (``memory_reply``/``memory_ack``/``memory_send``) therefore
# READ as its project yet WROTE as admin: reply-by-parent fails ("not in
# listen_set"), acks misfile onto the wrong reader, and sends go out under the
# wrong sender. Reported 2026-07-18 by projbeta / projalpha@macmini /
# admin@macmini — the read/write identity divergence bug.
#
# Two session-scoped anchors close this (the MCP bridge runs as ONE stdio
# subprocess per Claude session — same lifetime assumption ``_PRINCIPAL_CACHE``
# already relies on):
#
#   1. _STARTUP_CWD — the bridge's spawn working directory, captured once at
#      import. Claude Code now launches each stdio bridge with cwd = the
#      session's project root (verified 2026-07-18 across 5 live sessions; the
#      223b17b-era claim that ``os.getcwd()`` is "unrelated to the CC session" is
#      OBSOLETE — that is why project_dir became a hand-passed arg). This is the
#      reliable per-session anchor the arg was standing in for.
#
#   2. _SESSION_PROJECT_DIR — the last EXPLICIT project_dir a caller passed, an
#      override for the rare cross-project / cwd-changed case.
#
# CRUCIAL: neither anchor is "trusted as identity". Both are only the DIRECTORY
# that ``derive_project_name`` walks up from — and ``.engram.cfg`` is gold there:
# resolve_project_name returns the declared name whenever a cfg exists up the
# chain, and the raw dir basename is used ONLY as the pre-.engram.cfg bootstrap
# fallback (a brand-new project not yet configured). So a reliable cwd + an
# existing .engram.cfg = deterministically correct identity, with zero
# dependence on the LLM re-supplying project_dir per call.
try:
    _STARTUP_CWD: str | None = os.getcwd()
except OSError:  # pragma: no cover - cwd unlinked; nothing to anchor to
    _STARTUP_CWD = None

_SESSION_PROJECT_DIR: str | None = None

# The directory IDENTITY is derived from, as distinct from the one MEMORY is
# scoped to. Only a process that knows its own anchor authoritatively sets it
# (today: the watcher, from its `--project-dir` flag). The bridge never does —
# `_STARTUP_CWD` is already its authoritative anchor.
_IDENTITY_ANCHOR: str | None = None


def set_identity_anchor(project_dir: str | None) -> None:
    """Declare, once, the directory this PROCESS derives its identity from.

    For the bridge this is unnecessary: it is spawned with cwd = the session's
    project root, so ``_STARTUP_CWD`` is authoritative. The watcher is a
    separate process whose cwd is whatever shell launched it, so its
    ``--project-dir`` flag is the only authoritative anchor it has — it calls
    this at startup and the flag then wins over cwd for identity.

    Deliberately not driven by tool arguments: identity must be a property of
    the session, and anything a per-call argument can move is not one.
    """
    global _IDENTITY_ANCHOR
    if project_dir and os.path.isabs(project_dir):
        _IDENTITY_ANCHOR = project_dir


def identity_anchor_dir() -> str | None:
    """The directory to derive this session's IDENTITY from.

    Distinct from ``remember_project_dir``, and the distinction is the whole
    point. ``project_dir`` on a tool call answers *"which project's memory am I
    reading?"*; it must follow the call, because reading another project's
    memory is a normal, supported thing to do. It must NOT answer *"who am
    I?"* — yet until 2026-08-06 both were resolved through the same mutable
    pin, so ONE cross-project memory call silently re-derived the session's
    addresses for the rest of its life:

        start, pinned to ~/maintenance   -> ['admin', 'admin@macmini', ...]
        one call scoped elsewhere        -> ['engram', 'engram@macmini', ...]
        every call after, arg omitted    -> ['engram', 'engram@macmini', ...]

    The session kept its seat but left its own project GROUP and its
    ``<project>@<host>`` address. Mail to either was still accepted and stored,
    and the watcher — anchored by a flag, so it never moved — still WOKE the
    session; the session then read an inbox whose listen_set no longer
    contained the address the mail was sent to. Woken, and unable to find it.

    The module comment above called the override "the rare cross-project /
    cwd-changed case". Those are two cases, not one: a session that genuinely
    relocates should re-anchor, a session reading a peer's memory must not.
    Identity now resolves from a fixed anchor and cannot be moved by an
    argument; ``remember_project_dir`` keeps last-explicit-wins for memory.
    """
    return _IDENTITY_ANCHOR or _STARTUP_CWD or _SESSION_PROJECT_DIR


def remember_project_dir(project_dir: str | None) -> str | None:
    """Resolve the effective directory to derive identity from.

    Precedence:
      1. An explicit, usable (absolute) ``project_dir`` — honored and remembered
         as the session override (last explicit value wins, so a session that
         genuinely changes its working directory re-pins).
      2. The last explicit override remembered this session.
      3. ``_STARTUP_CWD`` — the bridge's spawn cwd (the session's project root).

    Only falls through to None on a cold session with no explicit arg AND no
    usable startup cwd, matching the pre-pin ``admin`` default. The returned
    directory is fed to ``derive_project_name``, where ``.engram.cfg`` (if any)
    is authoritative — this function chooses the *anchor*, not the identity.
    """
    global _SESSION_PROJECT_DIR
    if project_dir and os.path.isabs(project_dir):
        _SESSION_PROJECT_DIR = project_dir
        return project_dir
    return _SESSION_PROJECT_DIR or _STARTUP_CWD


def reset_session_pin() -> None:
    """Clear the explicit session override. For tests / process reuse only.

    Does not touch ``_STARTUP_CWD`` — that is the immutable spawn anchor.
    """
    global _SESSION_PROJECT_DIR
    _SESSION_PROJECT_DIR = None


def resolve_session_identity(project_dir: str | None) -> str | None:
    """The session's declared inbox identity, or None to use the project name.

    Precedence, most specific first:
      1. a runtime seat taken this session (``take_seat``) — deliberate, and
         later in time than anything decided at spawn
      2. ``ENGRAM_INBOX_IDENTITY`` — launch-time injection by a launcher
      3. ``inbox_identity`` in .engram.cfg — the durable per-FOLDER default

    Runtime outranks launch on purpose: a session only takes a seat because
    someone decided mid-flight that it is co-working, which is strictly newer
    information than whatever its spawn assumed. The tool that sets it reports
    when it overrides a launcher-set seat, so the override is never silent.
    """
    if _SESSION_SEAT:
        return _SESSION_SEAT
    # SEAT-2: a seat taken by our SIBLING process this session (the bridge, if
    # we are the watcher). Sits above the launch env for the same reason the
    # in-process seat does — it is a later, better-informed decision — and is
    # what lets a re-seat reach the watcher without restarting it.
    from_file = read_seat_file()
    if from_file:
        return from_file
    env = (os.environ.get(INBOX_IDENTITY_ENV) or "").strip().lower()
    declared = resolve_inbox_identity(project_dir)
    declared = declared.lower() if declared else None
    if env:
        # The precedence is correct and load-bearing: a launcher must be able
        # to seat each spawn distinctly, which is the only thing that keeps two
        # sessions in one folder from colliding. `.engram.cfg` is per-FOLDER
        # and cannot express that.
        #
        # What was wrong is the SILENCE. A repo that commits
        # `inbox_identity = X` has that declaration reviewed, version
        # controlled, and then discarded at runtime with no error, no warning
        # and nothing anywhere reporting a divergence — so the file says one
        # thing, the session is another, and mail to the declared name reaches
        # nobody. Measured 2026-08-02: a project declared `beastchat-server`,
        # ran as `beastchat-grok`, and the dead address went unnoticed long
        # enough for a peer to start writing remediation for the wrong cause.
        #
        # This module already holds the principle one level up — a runtime seat
        # that overrides a launcher-set one is reported, "so the override is
        # never silent". It simply was not applied to launch-over-file. It is
        # now: same rule, same reason, the other seam.
        if declared and declared != env:
            global _IDENTITY_OVERRIDE_NOTICE
            _IDENTITY_OVERRIDE_NOTICE = (
                f"'{declared}' (declared in .engram.cfg) is NOT in effect — "
                f"the launch environment set {INBOX_IDENTITY_ENV}='{env}', "
                f"which wins. Mail addressed to '{declared}' reaches nobody. "
                f"Either drop the .engram.cfg line or have the launcher honour "
                f"it; the env override itself is correct and is what keeps two "
                f"sessions in one folder distinct."
            )
        return env
    return declared


# Runtime seat, taken mid-session rather than injected at launch.
#
# Launch-time injection (ENGRAM_INBOX_IDENTITY) is the strongest mechanism and
# stays the default: a launcher that seats every spawn cannot forget, and the
# watcher inherits the same environ by process tree. But it only covers
# sessions a launcher started. A session opened by hand in a terminal — the
# common case when someone decides *after* starting that two agents should
# co-work in one folder — has no launcher to inject anything, and cannot
# re-exec itself. `.engram.cfg` is no help: it is per-FOLDER, and the whole
# problem is two sessions sharing one folder.
#
# So the seat is also settable at runtime, per session. Module-global is the
# correct scope: the bridge is one stdio subprocess per session (the same
# lifetime assumption `_PRINCIPAL_CACHE`, `_SESSION_NONCE` and
# `_SESSION_PROJECT_DIR` already rely on), so this cannot leak between
# sessions.
#
# ⚠ THE COST, stated plainly: a runtime seat moves the BRIDGE immediately, but
# the watcher is a separate process already running under the old environment.
# Until it is re-armed, the session is addressed at its new seat while still
# listening at the old one — it will not wake on DMs to the seat it just took.
# Project-addressed mail still arrives (the project group stays in both
# listen_sets), which is what makes this failure quiet rather than obvious.
# `take_seat` therefore returns the exact re-arm command and callers MUST run
# it; see server.memory_take_seat.
_SESSION_SEAT: str | None = None


# SEAT-2 — the seat FILE, which is what makes a runtime re-seat safe.
#
# A runtime seat moves the bridge instantly, but the watcher is a separate
# process that resolved its identity at start. Telling the session "remember to
# re-arm your watcher" is discipline, and discipline loses to inheritance.
#
# So the seat is also written to a file both processes can find from a value
# they ALREADY share: ENGRAM_SESSION_KEY, injected at spawn into the whole
# process tree. The watcher re-reads it every poll, so a re-seat propagates
# with no restart and no re-arm step. The split state stops being possible
# rather than being documented.
#
# Absent key → no file → both fall back to start-time env resolution, exactly
# as before. Hand-launched sessions do not regress; they just don't get the
# structural guarantee.
SESSION_KEY_ENV = "ENGRAM_SESSION_KEY"

# THE MARKER (SEAT-16). Every key this module GENERATES starts with this
# prefix, and that prefix is a contract: a generated key names a PROCESS
# (harness pid + start time), NOT a stable session handle. For a harness whose
# sessions are revivable — a dead-process reattach that reloads the same
# logical session into a fresh process — a generated key changes on every
# revive, so each revive claims a NEW seat. A consumer reading a key back
# (from /session/seats, a seat row, or the env) can therefore tell a
# process-lifetime identity from a launcher-injected stable one instead of
# inheriting the assumption that all keys survive respawn. Injected keys must
# NOT use this prefix; the server serves the distinction as
# ``session_key_generated`` on /session/seats.
AUTO_KEY_PREFIX = "auto-"


def _proc_info(pid: int) -> tuple[int, str] | None:
    """``(ppid, start_time)`` for ``pid`` via POSIX ps, or None.

    ``ps`` rather than /proc so one implementation covers macOS and Linux. The
    start time is opaque — we only need it to be stable for a process's
    lifetime and different after a PID is recycled.
    """
    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=,lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    parts = out.stdout.split()
    if len(parts) < 6:
        return None
    try:
        return int(parts[0]), " ".join(parts[1:6])
    except ValueError:
        return None


def auto_session_key_for(pid: int, start: str, host: str | None = None) -> str:
    """The session key naming the harness process ``pid``.

    Shared vocabulary between the bridge (which knows the harness is its own
    parent) and the watcher (which must rediscover it by walking ancestors), so
    both name the same session without a launcher.

    Carries ``AUTO_KEY_PREFIX`` deliberately: this key names a PROCESS, so it
    is only as durable as that process. The prefix is the marker that says so
    (SEAT-16) — never strip it, and never inject a key with this prefix.
    """
    host = host or hostname()
    safe = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in start).strip("-")
    return f"{AUTO_KEY_PREFIX}{host}-{pid}-{safe}".lower()


def derive_session_key() -> str | None:
    """This session's key when no launcher injected one, or None.

    The harness is this process's PARENT — reliable by construction, because a
    stdio MCP server is spawned as a direct child of the harness that talks to
    it (verified across the fleet's registration, which invokes the venv python
    directly with no shell wrapper).

    Deliberately NOT "walk up to pid 1": that reaches the tmux server, which is
    shared by every tmux session on the box, and would hand every session the
    same key — the precise opposite of the goal.

    The start time defeats PID reuse: a recycled pid yields a different key
    rather than silently inheriting a dead session's seat.
    """
    try:
        ppid = os.getppid()
    except OSError:  # pragma: no cover - no parent to anchor to
        return None
    if ppid <= 1:
        return None
    info = _proc_info(ppid)
    if info is None:
        return None
    return auto_session_key_for(ppid, info[1])


def resolve_session_key() -> str | None:
    """This session's key: launcher-injected, else derived.

    A launcher-injected key is preferred — AgentBeast's tmux name is unique per
    box and survives a respawn, which a pid cannot. The derived fallback gives
    a hand-launched session a unique key, NOT the same guarantees: it names
    the harness PROCESS, so it survives a bridge restart under a living
    harness but NOT a harness respawn. A harness that revives sessions
    (dead-process reattach into a fresh process) arrives with a new derived
    key each time and claims a new seat — measured 2026-08-10 as an ordinal
    pileup. Such a harness's launcher must inject a handle-derived
    ``ENGRAM_SESSION_KEY``; the derived key's ``AUTO_KEY_PREFIX`` is the
    marker that tells a consumer which kind it holds (SEAT-16).
    """
    env = (os.environ.get(SESSION_KEY_ENV) or "").strip().lower()
    if env:
        return env
    return derive_session_key()


# Where seat files live. Redirectable via ENGRAM_SEATS_DIR — which exists
# primarily so a TEST RUN CAN NEVER TOUCH A LIVE SESSION'S SEAT. take_seat()
# writes a real file, so a suite exercising seats inside a session that has a
# session key would otherwise rewrite that session's own inbox identity, and
# both its bridge and its watcher would silently start answering to whatever
# the last test happened to set. Found the hard way: a bridge test run
# reseated this very session to another project's address mid-suite.
SEATS_DIR_ENV = "ENGRAM_SEATS_DIR"
DEFAULT_SEATS_DIR = "~/.local/state/engram/seats"


def seats_dir() -> str:
    """Directory holding seat files."""
    return os.path.expanduser(
        (os.environ.get(SEATS_DIR_ENV) or "").strip() or DEFAULT_SEATS_DIR
    )


def seat_file_path(session_key: str | None = None) -> str | None:
    """Where this session's seat is recorded, or None without a key.

    Keyed on the session key rather than the project, because two sessions in
    ONE project folder is the entire case this exists for — a project-keyed
    path would collide exactly where it must not.
    """
    key = session_key or resolve_session_key()
    if not key:
        return None
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in key)[:128]
    if not safe:
        return None
    return os.path.join(seats_dir(), f"{safe}.seat")


_SEAT_ALLOWED = set("abcdefghijklmnopqrstuvwxyz0123456789._-")


def _is_valid_seat(value: str) -> bool:
    """Is this a usable inbox address?

    Seats are matched by EXACT STRING against listen_sets, so anything outside
    a plain address charset produces an address nobody listens on — a silent
    deafness rather than an error. Control characters are the dangerous case:
    they carry no whitespace and are non-empty, so a length-and-strip check
    waves them straight through (caught by test, not by review).
    """
    return bool(value) and len(value) <= 128 and set(value) <= _SEAT_ALLOWED


def _read_seat_at(path: str | None) -> str | None:
    """Read and validate one seat file. None on any problem."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            value = fh.read(256).strip().lower()
    except (OSError, UnicodeDecodeError):
        return None
    return value if _is_valid_seat(value) else None


# Cache of the ancestor-discovered seat PATH (not its contents). The seat inside
# can change — a re-seat is exactly what the watcher must follow — but which
# file belongs to this session cannot, so the walk runs once rather than every
# poll.
_DISCOVERED_SEAT_PATH: str | None = None


def discover_seat_file() -> str | None:
    """Find this session's seat file by walking up the process tree.

    The bridge names its seat file after the HARNESS process (its own parent).
    A watcher is a deeper descendant of that same harness — typically
    ``watcher → shell → harness`` — so the harness is on the watcher's ancestor
    chain, and the file the bridge wrote is discoverable without a launcher
    having injected anything.

    Nearest ancestor wins, which is the correct answer under nested harnesses.
    We probe for an EXISTING file rather than pattern-matching process names, so
    this needs no list of harness binaries to keep current.
    """
    global _DISCOVERED_SEAT_PATH
    if _DISCOVERED_SEAT_PATH and os.path.exists(_DISCOVERED_SEAT_PATH):
        return _DISCOVERED_SEAT_PATH
    _DISCOVERED_SEAT_PATH = None
    try:
        pid = os.getpid()
    except OSError:  # pragma: no cover
        return None
    host = hostname()
    seen: set[int] = set()
    for _ in range(12):  # bounded: never loop on a cyclic/odd tree
        if pid <= 1 or pid in seen:
            break
        seen.add(pid)
        info = _proc_info(pid)
        if info is None:
            break
        ppid, start = info
        path = seat_file_path(auto_session_key_for(pid, start, host))
        if path and os.path.exists(path):
            _DISCOVERED_SEAT_PATH = path
            return path
        pid = ppid
    return None


# Where the bridge records its harness's process identity for the watcher.
# Sits beside the seat file and is found the same two ways.
PROC_FILE_SUFFIX = ".proc"


def _proc_file_path(session_key: str | None = None) -> str | None:
    seat = seat_file_path(session_key)
    return seat[: -len(".seat")] + PROC_FILE_SUFFIX if seat else None


def record_session_process() -> str | None:
    """Record THIS BRIDGE's harness as the session process. Best-effort.

    Only the bridge can do this, and it is the whole reason the file exists.
    The bridge's parent IS the harness, reliable by construction — a stdio MCP
    server is spawned as a direct child of the harness that talks to it. The
    WATCHER has no such luck: measured on this fleet's shape,

        watcher  8431  ppid=8429  pgid=8429   ← its own process group
        wrapper  8429  ppid=6632  pgid=8429   ← /bin/zsh -c, group leader
        SESSION  6632  ppid=6411  pgid=6632   ← a DIFFERENT group

    its parent is a shell wrapper, an implementation detail of whatever armed
    it. Keying a death signal on that fails BOTH ways: when the session dies
    the wrapper is merely orphaned and keeps running (blocked in ``wait()``),
    so a parent check reports "alive" and the signal never fires; and killing
    the wrapper alone — which stopping a monitor does — reports the session
    gone while it is fine. The second is the expensive direction and it is
    routine, not exotic.

    Walking ancestors for a seat file does not rescue it either: that probe
    only matches AUTO-derived keys, so it finds hand-launched sessions and
    misses every launcher-injected one, which is most of this fleet.

    So the process that knows, writes it down. Stored as ``pid start_time``,
    because A PID ALONE IS AN ADDRESS, NOT AN IDENTITY — the OS recycles them,
    and a recycled pid reads as "still alive" forever. Same lesson as seat
    names, one layer down.
    """
    try:
        ppid = os.getppid()
    except OSError:  # pragma: no cover
        return None
    if ppid <= 1:
        return None  # no harness parent: nothing truthful to record
    info = _proc_info(ppid)
    if info is None:
        return None
    path = _proc_file_path()
    if not path:
        return None
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = f"{path}.tmp{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(f"{ppid} {info[1]}\n")
        os.replace(tmp, path)  # atomic: a poller never sees a half-written line
        return path
    except OSError:
        return None


def _read_proc_at(path: str | None) -> tuple[int, str] | None:
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read().strip()
    except OSError:
        return None
    pid, _, start = raw.partition(" ")
    try:
        return int(pid), start.strip()
    except ValueError:
        return None


def discover_session_process() -> tuple[int, str] | None:
    """``(pid, start_time)`` of the session this process belongs to, or None.

    Same two lookups as ``read_seat_file``: the resolved session key first
    (covers launcher-injected sessions, where bridge and watcher share
    ``ENGRAM_SESSION_KEY`` through the environment), then an ancestor probe
    (covers hand-launched ones, where the key is derived from the harness and
    the harness is therefore on the watcher's ancestor chain).

    None means "I could not identify a session to watch" — which must stay a
    distinct outcome from "the session is gone", never collapsed into it.
    """
    found = _read_proc_at(_proc_file_path())
    if found:
        return found
    try:
        pid = os.getpid()
    except OSError:  # pragma: no cover
        return None
    host = hostname()
    seen: set[int] = set()
    for _ in range(12):  # bounded: never loop on a cyclic/odd tree
        if pid <= 1 or pid in seen:
            break
        seen.add(pid)
        info = _proc_info(pid)
        if info is None:
            break
        ppid, start = info
        found = _read_proc_at(_proc_file_path(auto_session_key_for(pid, start, host)))
        if found:
            return found
        pid = ppid
    return None


def process_is_gone(pid: int, start: str) -> bool:
    """POSITIVE evidence that the process which was ``(pid, start)`` has exited.

    Asks the question in the direction that fails safe, which is the whole
    point. The obvious spelling — ``not process_is_alive(...)`` over
    ``_proc_info`` — has a hole we have now hit at three different layers:
    ``_proc_info`` returns None BOTH when the process is genuinely absent and
    when ``ps`` ITSELF FAILED (timeout, OSError, fork pressure). Reading that
    None as "gone" turns a transient hiccup into a death notice for a live
    session. ABSENT IS NOT DEAD, and "I could not ask" is not an answer.

    So: True only on a definite negative — ``ps`` ran and reported no such
    process, or reported a DIFFERENT process under a recycled pid. Every other
    outcome, including every failure to ask, returns False. Uncertainty can
    never produce a farewell.
    """
    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=,lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False  # could not ask — not evidence of anything
    parts = out.stdout.split()
    if len(parts) >= 6:
        # Something is running under this pid. It is OUR process only if the
        # start time matches; otherwise the pid was recycled and ours is gone.
        return " ".join(parts[1:6]) != start
    if out.returncode == 1:
        return True  # ps ran and found nothing: the one definite answer
    return False  # ps failed some other way — still not an answer


def read_seat_file() -> str | None:
    """The seat recorded for this session, or None.

    Two lookups, in order:
      1. our OWN session key — the bridge's own file, and any launcher-injected
         key, which the watcher inherits through the process tree
      2. ancestor discovery — the hand-launched case, where no launcher injected
         a key and the watcher's own derived key names its shell, not the
         harness

    Deliberately total: ANY problem — no key, missing file, unreadable,
    malformed, empty — returns None rather than raising. This is called from
    the watcher's poll loop, and a watcher that dies on a bad seat file is
    strictly worse than one listening on a stale seat: the stale one still
    catches project-addressed mail, the dead one catches nothing.
    """
    value = _read_seat_at(seat_file_path())
    if value:
        return value
    return _read_seat_at(discover_seat_file())


def _write_seat_file(seat: str) -> str | None:
    """Record the seat for peer processes. Best-effort; returns path or None.

    Failure here must never fail take_seat: the bridge is correctly seated
    either way, and the file is an optimisation that removes the re-arm step.
    """
    path = seat_file_path()
    if not path:
        return None
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = f"{path}.tmp{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(seat + "\n")
        os.replace(tmp, path)  # atomic: a poller never sees a half-written seat
        return path
    except OSError:
        return None


def take_seat(name: str) -> str:
    """Set this session's inbox seat at runtime. Returns the normalized seat.

    Normalizing here rather than at the call site is deliberate: seats are
    matched by exact string against listen_sets and participant lists, so a
    seat that only compares equal after someone else remembers to lowercase it
    is a seat that silently fails to receive mail.
    """
    global _SESSION_SEAT
    name = (name or "").strip().lower()
    if not name:
        raise ValueError("seat name is required")
    if not _is_valid_seat(name):
        raise ValueError(
            "seat must be lowercase letters, digits, dot, underscore or hyphen "
            "(it is matched as an exact inbox address)"
        )
    _SESSION_SEAT = name
    _write_seat_file(name)
    return name


def assert_local_seat(name: str) -> str:
    """Assert an identity for THIS PROCESS ONLY (WATCH-1's --identity).

    Same validation and precedence slot as ``take_seat`` — but it does NOT
    write the seat file. The file is the SESSION's shared identity, followed
    by the bridge; a watcher asserting its own identity through the file
    would re-seat the whole session, which is the inheritance hijack this
    flag exists to escape, pointed the other way. ``_SESSION_SEAT`` is a
    module global, so in a separate watcher process it scopes exactly to
    that process.
    """
    global _SESSION_SEAT
    name = (name or "").strip().lower()
    if not name:
        raise ValueError("seat name is required")
    if not _is_valid_seat(name):
        raise ValueError(
            "seat must be lowercase letters, digits, dot, underscore or hyphen "
            "(it is matched as an exact inbox address)"
        )
    _SESSION_SEAT = name
    return name


def current_seat() -> str | None:
    """The runtime seat this session has taken, if any."""
    return _SESSION_SEAT


def clear_seat() -> None:
    """Drop the runtime seat. For tests / process reuse only."""
    global _SESSION_SEAT
    _SESSION_SEAT = None


def hostname() -> str:
    return socket.gethostname().split(".")[0].lower()


def derive_project_name(project_dir: str | None) -> str:
    """Return the project name for this session.

    Resolution order:
        1. ``.engram.cfg`` walk-up (authoritative — survives prod/dev layouts)
        2. ``/projects/<name>/`` path segment (legacy convention)
        3. ``admin`` (machine-level work, scratch dirs, system paths, home)
    """
    declared = resolve_project_name(project_dir)
    if declared:
        return declared.lower()
    if not project_dir:
        return ADMIN_NAME
    parts = [p for p in project_dir.strip().split("/") if p]
    if "projects" in parts:
        idx = parts.index("projects")
        if idx + 1 < len(parts) and parts[idx + 1]:
            return parts[idx + 1].lower()
    return ADMIN_NAME


def is_admin_context(project_dir: str | None) -> bool:
    """True when the session should be treated as an admin session."""
    return derive_project_name(project_dir) == ADMIN_NAME


def admin_was_fallback(project_dir: str | None) -> bool:
    """True when ``admin`` was reached by FALLING THROUGH, not by declaration.

    ID-1: engram answers "what project is this?" with two resolvers of
    different strictness. Memory operations go through the strict one, which
    RAISES on an unconfigured directory so the tool layer can interrogate the
    user. Addressing goes through ``derive_project_name``, which silently
    falls back to ``admin`` — so a directory engram REFUSES to guess about
    for memory quietly adopts the administrator's identity for addressing,
    and the admin seat-exemption suppresses the one signal (a seat row) that
    would have shown it. A peer's probe session was nearly misfiled as a
    different bug because of exactly this.

    The fallback itself is CORRECT — home-dir and ``~/maintenance`` sessions
    share the admin identity on purpose, and an unconfigured session must
    still be able to heartbeat. What was wrong is the silence. This predicate
    is how the tool layer makes the fact visible: fallback admin (this
    returns True) gets announced once; declared admin (a cfg that says
    ``admin``, a ``/projects/admin/`` path) is a choice and stays quiet.
    """
    if derive_project_name(project_dir) != ADMIN_NAME:
        return False
    declared = resolve_project_name(project_dir)
    return not declared


def resolve_channels() -> list[str]:
    """Coalition channels this session subscribes to, from ``ENGRAM_CHANNELS``.

    Comma-separated, each entry MUST carry the ``#`` sigil (``"#devagents,#fleet"``).
    Entries without the sigil are dropped — a bare name is a *project* address,
    and silently promoting a typo into a channel subscription would collide
    with the flat project namespace.

    Env-only by design (docs/design/messaging-architecture.md §3.3–3.4): the
    project folder carries zero addressing, and channel membership is a LAUNCH
    concern injected by whatever spawns the session (launcher env, shell
    export). Authoritative membership lives in the roster via presence
    heartbeats — this env var is how a session *joins*; the roster is how
    membership is *seen*.
    """
    raw = os.environ.get("ENGRAM_CHANNELS", "")
    out: list[str] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if entry.startswith("#") and len(entry) > 1 and entry not in out:
            out.append(entry)
    return out


def resolve_provider() -> str:
    """Which harness is driving this bridge, from ``ENGRAM_PROVIDER``.

    The bridge is provider-neutral — Claude Code, Grok and Codex all spawn the
    SAME ``engram_mcp.server`` module out of the same venv, so the process
    cannot tell from the inside who launched it. The provider is therefore
    injected at launch, exactly like the seat (``ENGRAM_INBOX_IDENTITY``) and
    channel membership (``ENGRAM_CHANNELS``).

    Defaults to ``claude`` when unset. That default is deliberate
    back-compatibility, NOT a guess: this value was a hardcoded ``"claude"``
    literal in the presence heartbeat until 2026-07-23, so every session that
    predates launcher support keeps reporting exactly what it reported before.
    An unset value means "nobody told us", and the honest rendering of that is
    the historical default rather than a second unknown state for readers to
    interpret.

    Why it matters: the roster and the seat-collision detail both surface
    provider, and ``providers_seen`` exists specifically so a reader can tell
    two colliding sessions apart. While this was hardcoded, that field could
    only ever say ``["claude"]`` — including on a genuine Claude+Grok
    collision, the exact case it was built to disambiguate. A live reader was
    misled by it (agentbeast-app, 2026-07-23) and reported the constant to the
    owner as fact. A field that cannot vary is worse than a missing one.
    """
    return (os.environ.get("ENGRAM_PROVIDER") or "").strip().lower() or "claude"


def compute_identity(project_dir: str | None) -> tuple[str, list[str]]:
    """Return ``(reader_identity, listen_set)`` for the current call.

    - reader_identity uniquely names this session: ``<name>@<host>``
    - listen_set contains every address this session receives mail on:
        - the role/project name (loose broadcast: "any Claude with that role")
        - ``machine:<host>`` (loose broadcast: "any Claude on that machine")
        - the fully-qualified reader_identity itself (precise targeting)
        - any ``#channel`` subscriptions from ``ENGRAM_CHANNELS`` (appended
          last; see resolve_channels)

    Admin and project sessions are symmetric — the only difference is that
    admin's loose-broadcast name is the literal string ``admin``.

    When a session identity is declared (``ENGRAM_INBOX_IDENTITY`` env var or
    ``inbox_identity`` in .engram.cfg), the session keeps its project's group
    address but is precisely addressed (and sends) as ``<override>@<host>`` — so
    sibling sessions sharing one project get distinct inbox identities without
    splitting their shared memory.

    ``project_dir`` is first passed through the session pin
    (``remember_project_dir``): an explicit value is honored and remembered, an
    omitted one recalls the session's pinned dir. This keeps read and write
    identity consistent within a session even when the caller passes project_dir
    on some tool calls but not others (see the pin's module note).
    """
    # Memory scoping still follows the call — reading a peer's project is
    # normal and must keep working — so the pin is still updated here.
    remember_project_dir(project_dir)
    # IDENTITY does not. It resolves from a fixed anchor, so a cross-project
    # call cannot move this session's addresses out from under its watcher.
    host = hostname()
    project = derive_project_name(identity_anchor_dir())
    override = resolve_session_identity(project_dir) or ""
    channels = resolve_channels()
    # LANE-2 (docs/design/immortal-addresses.md): every session listens on its
    # implicit LANE — `<project>-<provider>`, the immortal mailbox for
    # "whoever is/next is the <provider> on this project" — IN ADDITION to its
    # occupant identity and its project channel (INV-1: nothing here displaces
    # the project addresses, and the occupant stays the From:/watcher-follow
    # target for occupant DMs). This is what lets mail outlive any one
    # session: a respawn hears the lane no matter which occupant seat it was
    # granted. An injected ENGRAM_INBOX_IDENTITY equal to the lane string is
    # exactly this address — the lane to listen on, not a seat to keep
    # (server-side reservation, LANE-1, refuses to mint it once flipped).
    # Admin is exempt end-to-end: no `admin-<provider>` lanes (SEAT-ADMIN-1).
    # The watcher resolves through this same function, so arming follows —
    # a watcher that only heard the occupant would miss lane mail, which is
    # the failure this step exists to end.
    lane = ""
    if project not in ADMIN_EXEMPT_LANE_PROJECTS:
        candidate = f"{project}-{resolve_provider()}"
        if candidate != project and _is_valid_seat(candidate):
            lane = candidate
    # GROUP-1: folder-declared TEAM addresses (`groups =` in .engram.cfg).
    # Every session in this folder listens on each — whatever seat a launcher
    # injected — so a peer's send to the team's natural name reaches the whole
    # team instead of whichever session happened to win that exact seat
    # string. Anchored like identity (the fixed anchor, not the per-call
    # project_dir) so a cross-project memory call cannot move them.
    groups: list[str] = []
    for g in resolve_inbox_groups(identity_anchor_dir()):
        if g != project and g != override and g != lane and _is_valid_seat(g):
            groups.append(g)
            groups.append(f"{g}@{host}")
    if override and override != project:
        reader = f"{override}@{host}"
        # precise identity first, then the project GROUP address (broadcasts to
        # all sessions on the project still land), then the project's
        # HOST-QUALIFIED group address, then machine, then self, then coalition
        # channels.
        #
        # `<project>@<host>` is load-bearing and was missing here until
        # 2026-08-06. It is this module's documented contract (see the header:
        # an admin session listens on ``admin@<host>``) and the convention the
        # operator addresses by: `admin@webone` and `admin@macmini` name the
        # maintenance session on each box, distinctly, without anyone knowing
        # what seat a launcher happened to assign.
        #
        # An unseated session got it from the branch below. A SEATED session
        # did not — so the moment a launcher began injecting
        # ENGRAM_INBOX_IDENTITY for every session it spawned, that address
        # silently stopped existing fleet-wide. Nothing rejected mail sent to
        # it; there was simply no longer a listener, which is the quietest way
        # for an address to die. Seats were the right change; dropping the
        # address underneath them was not.
        #
        # Additive by construction: the seat, the seat@host, the group and the
        # box are all still here, so restoring this cannot regress addressing
        # that works today.
        # LANE-2: the lane rides between the occupant and the project channel
        # (occupant, lane, lane@host, project, project@host, ...). Deduped
        # against the occupant: pre-reservation a granted seat can BE the
        # bare lane string, and one address must not appear twice.
        lane_entries = (
            [lane, f"{lane}@{host}"] if lane and lane != override else []
        )
        return (
            reader,
            [
                override,
                *lane_entries,
                project,
                f"{project}@{host}",
                *groups,
                f"machine:{host}",
                reader,
                *channels,
            ],
        )
    reader = f"{project}@{host}"
    # LANE-2 applies to unseated sessions too: a bare hand-launched session is
    # still "the <provider> on this project" and must hear its lane.
    lane_entries = [lane, f"{lane}@{host}"] if lane else []
    return (reader, [project, *lane_entries, *groups,
                     f"machine:{host}", reader, *channels])


def sender_lane(project_dir: str | None) -> str:
    """This session's immortal LANE — the address a reply should target.

    LANE-5: `<project>-<provider>`, the mailbox that outlives any one
    session. Stamped onto outgoing mail (like listen_set) so recipients'
    replies can route to the lane instead of the mortal seat: a reply
    composed after this session dies still reaches the lane's next occupant.

    Empty for admin (no admin-<provider> lanes ever, SEAT-ADMIN-1) and for
    anything that fails seat validation — an empty stamp means "route
    replies the legacy way," never an error.
    """
    project = derive_project_name(identity_anchor_dir() or project_dir)
    if project in ADMIN_EXEMPT_LANE_PROJECTS:
        return ""
    lane = f"{project}-{resolve_provider()}"
    return lane if lane != project and _is_valid_seat(lane) else ""


def reader_to_address(reader_identity: str) -> str:
    """Convert a reader_identity back to its loose-broadcast address.

    Reader identities have shape ``<name>@<host>``; the loose address is the
    ``<name>`` part. Legacy ``machine:<host>`` identities (pre-admin rollout)
    pass through unchanged for backward compatibility with already-sent mail.

    This is how ``memory_reply`` recovers the ``to:`` for a reply. Be precise
    about what that yields: the name-part of the reader_identity. For an
    UNSEATED sender that is the project (a loose, immortal address); for a
    SEATED sender it is the SEAT — the mortal per-session ordinal, not the
    project and not a lane. Adversarial review (2026-08-14) measured deployed
    bridges relying on exactly this, so it is the wire contract until LANE-5
    flips the default to reply-to-lane behind the WIRE-1 gates
    (docs/design/immortal-addresses.md). An earlier version of this docstring
    claimed "replies go to the sender's role, not their specific session" —
    that was aspiration, not behavior.
    """
    if not reader_identity:
        return reader_identity
    if reader_identity.startswith("machine:"):
        return reader_identity
    if "@" in reader_identity:
        return reader_identity.split("@", 1)[0]
    return reader_identity
