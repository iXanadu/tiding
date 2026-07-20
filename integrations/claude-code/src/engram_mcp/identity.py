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

from engram_mcp.scoping import resolve_inbox_identity, resolve_project_name

ADMIN_NAME = "admin"

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

    Env var wins over .engram.cfg so a session can override the file on the fly.
    """
    env = (os.environ.get(INBOX_IDENTITY_ENV) or "").strip().lower()
    if env:
        return env
    declared = resolve_inbox_identity(project_dir)
    return declared.lower() if declared else None


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
    project_dir = remember_project_dir(project_dir)
    host = hostname()
    project = derive_project_name(project_dir)
    override = resolve_session_identity(project_dir) or ""
    channels = resolve_channels()
    if override and override != project:
        reader = f"{override}@{host}"
        # precise identity first, then the project GROUP address (broadcasts to
        # all sessions on the project still land), then machine, then self,
        # then coalition channels.
        return (reader, [override, project, f"machine:{host}", reader, *channels])
    reader = f"{project}@{host}"
    return (reader, [project, f"machine:{host}", reader, *channels])


def reader_to_address(reader_identity: str) -> str:
    """Convert a reader_identity back to its loose-broadcast address.

    Reader identities have shape ``<name>@<host>``; the loose address is the
    ``<name>`` part. Legacy ``machine:<host>`` identities (pre-admin rollout)
    pass through unchanged for backward compatibility with already-sent mail.

    This is how ``memory_reply`` recovers the right ``to:`` for a reply —
    replies go to the sender's *role*, not their specific session.
    """
    if not reader_identity:
        return reader_identity
    if reader_identity.startswith("machine:"):
        return reader_identity
    if "@" in reader_identity:
        return reader_identity.split("@", 1)[0]
    return reader_identity
